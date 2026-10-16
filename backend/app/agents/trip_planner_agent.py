"""多智能体旅行规划系统(LangChain + LangGraph 版本)

架构:
    1. 三路检索(景点 / 天气 / 酒店)是**确定性**的:由 LangGraph 并行调度,
       直接调高德 MCP 工具 + 服务层解析,不经过模型(见 services/trip_retrieval.py);
    2. 唯一的模型调用是"规划型"Agent:汇总三路结果,输出结构化 ``TripPlan``;
    3. 对外仍暴露 ``plan_trip(request) -> TripPlan``,与旧实现保持一致,
       因此 FastAPI 路由与前端契约无需改动。

与 HelloAgents 版本的关键差异:
    - 不再使用 ``[TOOL_CALL:...]`` 文本协议,改为原生 function calling
    - 不再手工从文本里抠 JSON,改为 ``response_format=TripPlan`` 结构化输出
    - 三个检索节点并行执行,而非串行阻塞
"""

from __future__ import annotations

import asyncio
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, TypedDict

from langchain.agents import create_agent
from langgraph.graph import END, START, StateGraph

from ..config import get_settings
from ..models.schemas import (
    Attraction,
    DayPlan,
    Location,
    Meal,
    TripPlan,
    TripRequest,
)
from ..services.amap_mcp import get_amap_tools, root_cause
from ..services.llm_service import get_llm
from ..services.trip_retrieval import (
    retrieve_attractions,
    retrieve_hotels,
    retrieve_weather,
)
from ..utils.json_utils import extract_json

# 整张图的 super-step 上限。刻意比单 Agent 的 agent_recursion_limit 宽松得多:
# 三路检索并行 + 规划,每个子 Agent 的步数都会累加到同一个计数器上,
# 真正约束单个 Agent 行为的是它自己那份 config(见 _run_agent)。
GRAPH_RECURSION_LIMIT = 60

# ============ 提示词 ============
# 只有"规划"这一步需要模型:三路检索已经改成服务层确定性调用
# (见 services/trip_retrieval.py),所以不再需要检索型 Agent 的提示词。

PLANNER_AGENT_PROMPT = """你是行程规划专家。你的任务是根据景点、天气、酒店信息,生成详细的旅行计划。

请严格按以下 JSON 结构返回:
```json
{
  "city": "城市名称",
  "start_date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD",
  "days": [
    {
      "date": "YYYY-MM-DD",
      "day_index": 0,
      "description": "第1天行程概述",
      "transportation": "交通方式",
      "accommodation": "住宿类型",
      "hotel": {
        "name": "酒店名称",
        "address": "酒店地址",
        "location": {"longitude": 116.397128, "latitude": 39.916527},
        "price_range": "300-500元",
        "rating": "4.5",
        "distance": "距离景点2公里",
        "type": "经济型酒店",
        "estimated_cost": 400
      },
      "attractions": [
        {
          "name": "景点名称",
          "address": "详细地址",
          "location": {"longitude": 116.397128, "latitude": 39.916527},
          "visit_duration": 120,
          "description": "景点详细描述",
          "category": "景点类别",
          "ticket_price": 60
        }
      ],
      "meals": [
        {"type": "breakfast", "name": "早餐推荐", "description": "早餐描述", "estimated_cost": 30},
        {"type": "lunch", "name": "午餐推荐", "description": "午餐描述", "estimated_cost": 50},
        {"type": "dinner", "name": "晚餐推荐", "description": "晚餐描述", "estimated_cost": 80}
      ]
    }
  ],
  "weather_info": [
    {
      "date": "YYYY-MM-DD",
      "day_weather": "晴",
      "night_weather": "多云",
      "day_temp": 25,
      "night_temp": 15,
      "wind_direction": "南风",
      "wind_power": "1-3级"
    }
  ],
  "overall_suggestions": "总体建议",
  "budget": {
    "total_attractions": 180,
    "total_hotels": 1200,
    "total_meals": 480,
    "total_transportation": 200,
    "total": 2060
  }
}
```

**重要提示:**
1. weather_info 数组必须包含每一天的天气信息
2. 温度必须是纯数字(不要带 ℃ 等单位)
3. 每天安排 2-3 个景点
4. 考虑景点之间的距离和游览时间
5. 每天必须包含早、中、晚三餐
6. 每天推荐一家具体酒店(从酒店信息中选择)
7. 提供实用的旅行建议
8. **必须包含预算信息**:景点门票(ticket_price)、餐饮预估费用(estimated_cost)、
   酒店预估费用(estimated_cost)、预算汇总(budget)
9. 景点经纬度必须真实准确
"""


class TripPlanningState(TypedDict, total=False):
    """LangGraph 状态

    - request: 原始请求(只读)
    - *_result: 三路检索的文本结果
    - plan: 最终结构化行程
    """

    request: TripRequest
    attraction_result: str
    weather_result: str
    hotel_result: str
    plan: Optional[TripPlan]


@dataclass
class PlanOutcome:
    """一次规划的完整结果(含"是否降级"标记)。

    对外接口 :meth:`MultiAgentTripPlanner.plan_trip` 仍然只返回 ``TripPlan``
    (保持旧契约);需要区分"真实规划"与"兜底占位行程"的调用方(HTTP 路由、
    测试)请改用 :meth:`plan_trip_with_status`。

    Attributes:
        plan: 行程数据。``degraded=True`` 时是占位内容。
        degraded: 是否走了兜底路径。
        reason: 降级原因(便于日志与前端提示)。
    """

    plan: TripPlan
    degraded: bool = False
    reason: str = ""


def _last_message_text(result: Dict[str, Any]) -> str:
    """从 ``create_agent`` 的返回值中取出最后一条消息的文本。"""
    messages = result.get("messages") or []
    if not messages:
        return ""

    content = getattr(messages[-1], "content", messages[-1])

    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)

    return content or ""


def _as_datetime(value: Any) -> datetime:
    """把 ``TripRequest`` 的日期字段统一成 ``datetime``。

    字段类型历史上是 ``str``,现在是 ``date``(也有可能是 ``datetime``),
    三种形式都兼容,避免兜底路径在类型变化后抛异常。
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return datetime.strptime(str(value), "%Y-%m-%d")


class MultiAgentTripPlanner:
    """多智能体旅行规划系统(LangChain + LangGraph)"""

    AGENT_NAME = "多智能体旅行规划系统(LangChain)"
    GRAPH_NODES = [
        "search_attractions",
        "query_weather",
        "search_hotels",
        "generate_plan",
    ]

    def __init__(self, llm: Any = None):
        """初始化系统

        LLM 立即初始化以尽早暴露配置错误;高德 MCP 工具与 LangGraph
        则在首次 ``plan_trip`` 时惰性构建(避免启动时就拉起 uvx 子进程)。
        """
        print("🔄 开始初始化多智能体旅行规划系统(LangChain)...")

        self.llm = llm or get_llm()
        self._tools: List[Any] = []
        self._graph: Any = None
        self._planner_fallback: Any = None
        self._init_lock = asyncio.Lock()

        print("✅ 多智能体系统初始化成功(工具与图将在首次调用时构建)")

    # ---------- 初始化 ----------

    async def _ensure_graph(self) -> Any:
        """惰性构建工具、Agent 与 LangGraph(并发安全)。"""
        if self._graph is not None:
            return self._graph

        async with self._init_lock:
            if self._graph is not None:
                return self._graph

            print("  - 加载高德 MCP 工具...")
            self._tools = await get_amap_tools()

            print("  - 创建行程规划 Agent(结构化输出)...")
            self.planner_agent = create_agent(
                model=self.llm,
                tools=[],
                system_prompt=PLANNER_AGENT_PROMPT,
                response_format=TripPlan,
                name="planner_agent",
            )
            # 结构化输出不被模型支持时的兜底:纯文本 + JSON 解析
            self._planner_fallback = create_agent(
                model=self.llm,
                tools=[],
                system_prompt=PLANNER_AGENT_PROMPT,
                name="planner_fallback",
            )

            print("  - 编译 LangGraph...")
            builder = StateGraph(TripPlanningState)
            builder.add_node("search_attractions", self._search_attractions)
            builder.add_node("query_weather", self._query_weather)
            builder.add_node("search_hotels", self._search_hotels)
            builder.add_node("generate_plan", self._generate_plan)

            # 三路检索并行 fan-out,再汇聚到 generate_plan
            for node in ("search_attractions", "query_weather", "search_hotels"):
                builder.add_edge(START, node)
                builder.add_edge(node, "generate_plan")
            builder.add_edge("generate_plan", END)

            self._graph = builder.compile()
            print(f"✅ LangGraph 编译完成,节点: {', '.join(self.GRAPH_NODES)}")
            return self._graph

    # ---------- 图节点 ----------

    async def _run_agent(self, agent: Any, query: str, label: str) -> str:
        """执行一个子 Agent,失败时返回空串而不中断整张图。

        带上 ``recursion_limit`` 是有意为之:结构化输出或工具调用一旦进入
        "把同一份参数原样重发"的循环,不加约束会一路烧到客户端超时
        (实测 6 次重试 100 秒,端到端 11 分钟不返回)。

        另:高德 MCP 走的是常驻 HTTP 连接,三路 Agent 并发时偶发连接被重置,
        客户端只会看到 anyio 包出来的 "unhandled errors in a TaskGroup"。
        这类瞬时失败重试一次即可,不该让整条检索分支直接变成空结果。
        """
        messages = {"messages": [{"role": "user", "content": query}]}
        config = {"recursion_limit": get_settings().agent_recursion_limit}

        for attempt in (1, 2):
            try:
                result = await agent.ainvoke(messages, config=config)
                return _last_message_text(result)
            except Exception as exc:
                # 用 root_cause 剥掉 ExceptionGroup/TaskGroup 包装,否则日志里
                # 只剩 "unhandled errors in a TaskGroup (1 sub-exception)",
                # 真正的报错(例如连接被重置)全被埋掉。
                reason = root_cause(exc)
                if attempt == 1:
                    print(f"⚠️  {label} 第 1 次执行失败,重试一次: {reason}")
                else:
                    print(f"⚠️  {label} 执行失败: {reason}")

        return ""

    async def _search_attractions(self, state: TripPlanningState) -> Dict[str, Any]:
        """景点检索:直接调高德工具 + 服务层解析,不经过模型。"""
        return {"attraction_result": await retrieve_attractions(state["request"])}

    async def _query_weather(self, state: TripPlanningState) -> Dict[str, Any]:
        """天气查询:直接用高德返回的结构化数据,不经过模型。"""
        return {"weather_result": await retrieve_weather(state["request"])}

    async def _search_hotels(self, state: TripPlanningState) -> Dict[str, Any]:
        """酒店检索:同上,确定性调用。"""
        return {"hotel_result": await retrieve_hotels(state["request"])}

    async def _generate_plan(self, state: TripPlanningState) -> Dict[str, Any]:
        request = state["request"]
        query = self._build_planner_query(
            request,
            state.get("attraction_result", ""),
            state.get("weather_result", ""),
            state.get("hotel_result", ""),
        )
        plan = await self._run_structured_planner(query, request)
        return {"plan": plan}

    async def _run_structured_planner(
        self, query: str, request: TripRequest
    ) -> Optional[TripPlan]:
        """优先使用结构化输出,失败则退回文本 + JSON 解析。

        解析不出来时返回 ``None``(而不是就地造一份兜底行程),
        这样"是否降级"的判断统一收敛到 :meth:`plan_trip_with_status`。
        """
        try:
            result = await self.planner_agent.ainvoke(
                {"messages": [{"role": "user", "content": query}]},
                config={"recursion_limit": get_settings().agent_recursion_limit},
            )
            structured = result.get("structured_response")
            if isinstance(structured, TripPlan):
                return structured
            if structured is not None:
                return TripPlan.model_validate(structured)
        except Exception as exc:
            print(f"⚠️  结构化输出失败,退回 JSON 解析: {exc}")

        response = await self._run_agent(self._planner_fallback, query, "行程规划(兜底)")
        return self._parse_response(response, request)

    # ---------- 对外接口 ----------

    async def plan_trip(self, request: TripRequest) -> TripPlan:
        """使用多智能体协作生成旅行计划(兼容旧契约,只返回 TripPlan)

        Args:
            request: 旅行请求

        Returns:
            旅行计划。**失败时返回的是占位行程**,调用方若需要区分,
            请改用 :meth:`plan_trip_with_status`。
        """
        outcome = await self.plan_trip_with_status(request)
        return outcome.plan

    async def plan_trip_with_status(self, request: TripRequest) -> PlanOutcome:
        """使用多智能体协作生成旅行计划,并返回降级标记

        与 ``plan_trip`` 的区别只在于返回值:这里会显式告诉调用方
        结果是不是兜底占位行程,避免把 "AI 失败" 当成 "规划成功" 返回。

        Args:
            request: 旅行请求

        Returns:
            :class:`PlanOutcome`,其中 ``degraded=True`` 表示走了兜底路径。
        """
        try:
            print(f"\n{'=' * 60}")
            print("🚀 开始多智能体协作规划旅行...")
            print(f"目的地: {request.city}")
            print(f"日期: {request.start_date} 至 {request.end_date}")
            print(f"天数: {request.travel_days}天")
            prefs = ", ".join(request.preferences) if request.preferences else "无"
            print(f"偏好: {prefs}")
            print(f"{'=' * 60}\n")

            graph = await self._ensure_graph()
            settings = get_settings()

            # LLM_TIMEOUT 只约束"两次读取之间的间隔",对流式响应不构成总时长约束,
            # 所以这里再加一道总闸:宁可早降级,也不把客户端超时打满。
            try:
                final_state = await asyncio.wait_for(
                    graph.ainvoke(
                        {"request": request},
                        config={"recursion_limit": GRAPH_RECURSION_LIMIT},
                    ),
                    timeout=settings.plan_total_timeout,
                )
            except asyncio.TimeoutError:
                return self._degrade(
                    request, f"规划超时(超过 {settings.plan_total_timeout} 秒,已放弃)"
                )

            results = (
                final_state.get("attraction_result"),
                final_state.get("weather_result"),
                final_state.get("hotel_result"),
            )
            if not any(results):
                return self._degrade(request, "三路检索均无结果")

            plan = final_state.get("plan")
            if not isinstance(plan, TripPlan):
                return self._degrade(request, "规划 Agent 未返回可用的结构化行程")

            print(f"{'=' * 60}")
            print("✅ 旅行计划生成完成!")
            print(f"{'=' * 60}\n")
            return PlanOutcome(plan=plan)

        except Exception as exc:
            print(f"❌ 生成旅行计划失败: {exc}")
            traceback.print_exc()
            return self._degrade(request, f"规划过程出错: {exc}")

    def _degrade(self, request: TripRequest, reason: str) -> PlanOutcome:
        """返回兜底占位行程,并显式标记为降级结果。"""
        print(f"⚠️  使用兜底占位行程({reason})")
        return PlanOutcome(
            plan=self._create_fallback_plan(request),
            degraded=True,
            reason=reason,
        )

    # ---------- 查询构建 ----------

    def _build_planner_query(
        self, request: TripRequest, attractions: str, weather: str, hotels: str = ""
    ) -> str:
        """构建行程规划查询"""
        prefs = ", ".join(request.preferences) if request.preferences else "无"
        query = f"""请根据以下信息生成{request.city}的{request.travel_days}天旅行计划

**基本信息:**
- 城市: {request.city}
- 日期: {request.start_date} 至 {request.end_date}
- 天数: {request.travel_days}天
- 交通方式: {request.transportation}
- 住宿: {request.accommodation}
- 偏好: {prefs}

**景点信息:**
{attractions}

**天气信息:**
{weather}

**酒店信息:**
{hotels}

**要求:**
1. 每天安排2-3个景点
2. 每天必须包含早中晚三餐
3. 每天推荐一个具体的酒店(从酒店信息中选择)
4. 考虑景点之间的距离和交通方式
5. 返回完整的JSON格式数据
6. 景点的经纬度坐标要真实准确
"""
        if request.free_text_input:
            query += f"\n**额外要求:** {request.free_text_input}"

        return query

    # ---------- 结果解析与兜底 ----------

    def _parse_response(self, response: str, request: TripRequest) -> Optional[TripPlan]:
        """解析 Agent 响应中的 JSON 为 TripPlan。

        这是结构化输出不可用时的兜底路径;解析失败返回 ``None``,
        由调用方决定是否降级为占位行程。
        """
        data = extract_json(response)
        if not isinstance(data, dict):
            print("⚠️  响应中未找到 JSON 数据")
            return None

        try:
            return TripPlan(**data)
        except Exception as exc:
            print(f"⚠️  解析响应失败: {exc}")
            return None

    def _create_fallback_plan(self, request: TripRequest) -> TripPlan:
        """创建占位行程(当 Agent 失败时)

        注意:这里生成的是**占位内容**(景点名形如 "北京景点1"),
        只用于让前端仍能渲染出页面结构,不代表真实的行程规划结果。
        对外会通过 ``TripPlanResponse.degraded=True`` 明确标记。
        """
        start_date = _as_datetime(request.start_date)
        end_date = _as_datetime(request.end_date)

        days = []
        for i in range(request.travel_days):
            current_date = start_date + timedelta(days=i)

            day_plan = DayPlan(
                date=current_date.strftime("%Y-%m-%d"),
                day_index=i,
                description=f"第{i + 1}天行程",
                transportation=request.transportation,
                accommodation=request.accommodation,
                attractions=[
                    Attraction(
                        name=f"{request.city}景点{j + 1}",
                        address=f"{request.city}市",
                        location=Location(
                            longitude=116.4 + i * 0.01 + j * 0.005,
                            latitude=39.9 + i * 0.01 + j * 0.005,
                        ),
                        visit_duration=120,
                        description=f"这是{request.city}的著名景点",
                        category="景点",
                    )
                    for j in range(2)
                ],
                meals=[
                    Meal(type="breakfast", name=f"第{i + 1}天早餐", description="当地特色早餐"),
                    Meal(type="lunch", name=f"第{i + 1}天午餐", description="午餐推荐"),
                    Meal(type="dinner", name=f"第{i + 1}天晚餐", description="晚餐推荐"),
                ],
            )
            days.append(day_plan)

        return TripPlan(
            city=request.city,
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d"),
            days=days,
            weather_info=[],
            overall_suggestions=(
                f"⚠️ 本次 AI 规划未成功,以下为占位行程(景点与餐饮名称均为占位符),"
                f"请勿直接采用,建议稍后重试。\n"
                f"这是为您规划的{request.city}{request.travel_days}日游行程,"
                f"建议提前查看各景点的开放时间。"
            ),
        )

    # ---------- 运行时信息 ----------

    def describe(self) -> Dict[str, Any]:
        """返回运行时信息(供健康检查使用,不会触发工具加载)。"""
        model = getattr(self.llm, "model_name", None) or getattr(self.llm, "model", "")
        return {
            "agent_name": self.AGENT_NAME,
            "framework": "langchain+langgraph",
            "model": model or "",
            "graph_nodes": self.GRAPH_NODES,
            "graph_ready": self._graph is not None,
            "tools_loaded": len(self._tools),
        }


# 全局多智能体系统实例
_multi_agent_planner: Optional[MultiAgentTripPlanner] = None


def get_trip_planner_agent() -> MultiAgentTripPlanner:
    """获取多智能体旅行规划系统实例(单例模式)"""
    global _multi_agent_planner

    if _multi_agent_planner is None:
        _multi_agent_planner = MultiAgentTripPlanner()

    return _multi_agent_planner


def reset_trip_planner_agent() -> None:
    """重置全局实例(用于测试或重新配置)"""
    global _multi_agent_planner
    _multi_agent_planner = None
