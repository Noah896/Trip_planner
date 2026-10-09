"""多智能体旅行规划系统(LangChain + LangGraph 版本)

架构:
    1. 三个"检索型"Agent(景点 / 天气 / 酒店),各自挂载高德 MCP 工具,
       由 LangGraph 并行调度(三路互不依赖);
    2. 一个"规划型"Agent 汇总三路结果,输出结构化 ``TripPlan``;
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
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, TypedDict

from langchain.agents import create_agent
from langgraph.graph import END, START, StateGraph

from ..models.schemas import (
    Attraction,
    DayPlan,
    Location,
    Meal,
    TripPlan,
    TripRequest,
)
from ..services.amap_mcp import get_amap_tools
from ..services.llm_service import get_llm
from ..utils.json_utils import extract_json

# ============ Agent 提示词 ============
# 注意:相比 HelloAgents 版本,这里删掉了整段 "[TOOL_CALL:...]" 格式说明
# —— LangChain 通过原生 function calling 调度工具,无需在提示词里教模型写协议。

ATTRACTION_AGENT_PROMPT = """你是景点搜索专家。你的任务是根据城市和用户偏好,调用高德地图工具检索合适的景点。

**重要提示:**
必须调用工具获取真实景点数据,不要凭记忆编造景点。

**要求:**
1. 检索关键词要贴合用户偏好(如"历史文化""美食""公园")
2. 如果一次结果太少,可以换关键词再检索一次
3. 最终用简洁的中文逐条列出候选景点,每条包含:
   名称 / 地址 / 经纬度 / 类别 / 建议游览时长(分钟) / 门票价格(元) / 一句话描述
"""

WEATHER_AGENT_PROMPT = """你是天气查询专家。你的任务是用高德地图天气工具查询指定城市未来几天的天气。

**重要提示:**
必须调用工具查询,不要编造天气数据。

**要求:**
温度只写数字(不要带 ℃)。逐条列出每天的:日期 / 白天天气 / 夜间天气 / 白天温度 / 夜间温度 / 风向 / 风力。
"""

HOTEL_AGENT_PROMPT = """你是酒店推荐专家。你的任务是根据城市和住宿偏好,用高德地图工具检索合适的酒店。

**重要提示:**
必须调用工具检索,不要编造酒店。

**要求:**
推荐 3-5 家酒店,每家给出:名称 / 地址 / 经纬度 / 类型 / 参考价格区间 / 评分,并简单说明适合人群。
"""

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

            print("  - 创建景点检索 Agent...")
            self.attraction_agent = create_agent(
                model=self.llm,
                tools=self._tools,
                system_prompt=ATTRACTION_AGENT_PROMPT,
                name="attraction_agent",
            )

            print("  - 创建天气查询 Agent...")
            self.weather_agent = create_agent(
                model=self.llm,
                tools=self._tools,
                system_prompt=WEATHER_AGENT_PROMPT,
                name="weather_agent",
            )

            print("  - 创建酒店推荐 Agent...")
            self.hotel_agent = create_agent(
                model=self.llm,
                tools=self._tools,
                system_prompt=HOTEL_AGENT_PROMPT,
                name="hotel_agent",
            )

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
        """执行一个子 Agent,失败时返回空串而不中断整张图。"""
        try:
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": query}]}
            )
            return _last_message_text(result)
        except Exception as exc:
            print(f"⚠️  {label} 执行失败: {exc}")
            return ""

    async def _search_attractions(self, state: TripPlanningState) -> Dict[str, Any]:
        request = state["request"]
        text = await self._run_agent(
            self.attraction_agent, self._build_attraction_query(request), "景点检索"
        )
        return {"attraction_result": text}

    async def _query_weather(self, state: TripPlanningState) -> Dict[str, Any]:
        request = state["request"]
        text = await self._run_agent(
            self.weather_agent, f"请查询{request.city}的天气信息", "天气查询"
        )
        return {"weather_result": text}

    async def _search_hotels(self, state: TripPlanningState) -> Dict[str, Any]:
        request = state["request"]
        text = await self._run_agent(
            self.hotel_agent,
            f"请搜索{request.city}的{request.accommodation}酒店",
            "酒店检索",
        )
        return {"hotel_result": text}

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
    ) -> TripPlan:
        """优先使用结构化输出,失败则退回文本 + JSON 解析。"""
        try:
            result = await self.planner_agent.ainvoke(
                {"messages": [{"role": "user", "content": query}]}
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
        """使用多智能体协作生成旅行计划

        Args:
            request: 旅行请求

        Returns:
            旅行计划(失败时返回备用方案)
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
            final_state = await graph.ainvoke({"request": request})

            results = (
                final_state.get("attraction_result"),
                final_state.get("weather_result"),
                final_state.get("hotel_result"),
            )
            if not any(results):
                print("⚠️  三路检索均无结果,使用备用方案")
                return self._create_fallback_plan(request)

            plan = final_state.get("plan")
            if not isinstance(plan, TripPlan):
                return self._create_fallback_plan(request)

            print(f"{'=' * 60}")
            print("✅ 旅行计划生成完成!")
            print(f"{'=' * 60}\n")
            return plan

        except Exception as exc:
            print(f"❌ 生成旅行计划失败: {exc}")
            traceback.print_exc()
            return self._create_fallback_plan(request)

    # ---------- 查询构建 ----------

    def _build_attraction_query(self, request: TripRequest) -> str:
        """构建景点检索查询(自然语言,LangChain 会自行决定调用工具)。"""
        keywords = request.preferences[0] if request.preferences else "景点"
        return (
            f"请调用高德地图工具搜索{request.city}的{keywords}相关景点,"
            f"并按你的要求整理候选景点清单。"
        )

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

    def _parse_response(self, response: str, request: TripRequest) -> TripPlan:
        """解析 Agent 响应中的 JSON 为 TripPlan。

        这是结构化输出不可用时的兜底路径。
        """
        data = extract_json(response)
        if not isinstance(data, dict):
            print("⚠️  响应中未找到 JSON 数据,将使用备用方案")
            return self._create_fallback_plan(request)

        try:
            return TripPlan(**data)
        except Exception as exc:
            print(f"⚠️  解析响应失败: {exc}")
            return self._create_fallback_plan(request)

    def _create_fallback_plan(self, request: TripRequest) -> TripPlan:
        """创建备用计划(当 Agent 失败时)"""
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
