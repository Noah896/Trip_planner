"""聊天式入口:把用户的一句话变成"一次追问"或"一份行程"。

设计要点(完整推导见 AGENTS.md 第 5 节)
----------------------------------------

1. **意图识别与槽位抽取只用一次模型调用**。常见做法是"先分类、再回答"(两次),
   但这里的意图和槽位是同一件事的两面 —— 一次扁平的结构化输出就能同时给出
   「意图 / 抽到的条件 / 给用户的回复 / 还缺什么」。
2. **结构化直传走确定性短路**:客户端已经带齐表单(`ChatRequest.trip`)时,
   跳过意图识别,一次模型调用都不额外花 —— 这就是"聊天入口和表单入口共用
   一套流水线"的接缝。
3. **规划本身完全复用现有链路**:`plan_trip_stream()` 的事件直接转换过来,
   所以降级判断、超时闸门、SSE 进度这些都不用重写。
4. 槽位里的日期换算交给模型(相对时间需要"今天"作参照),但**拼装
   `TripRequest` 是确定性的**:模型给什么就填什么,给不全就只追问,绝不替它编。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

from langchain.agents import create_agent
from pydantic import BaseModel, Field, field_validator

from ..agents.trip_planner_agent import get_trip_planner_agent
from ..config import get_settings
from ..models.schemas import ChatMessage, ChatRequest, TripRequest
from .llm_service import get_llm

# 模型没提到这些字段时用的兜底值(与表单页的默认值保持一致)
DEFAULT_TRANSPORTATION = "公共交通"
DEFAULT_ACCOMMODATION = "经济型酒店"

# 喂给意图识别的历史最多保留多少条(再长也没用,只推高成本)
MAX_HISTORY = 10


class ChatDecision(BaseModel):
    """一次模型调用的产出:意图 + 抽到的槽位 + 回复 + 还缺什么。

    **刻意做成扁平结构**:嵌套对象正是我们踩过的坑 —— 模型会把嵌套对象写成
    JSON 字符串,Pydantic 校验失败后 LangChain 回灌错误让它重试,而它会把
    一模一样的参数再发一遍,一路重试到 recursion_limit(见第二轮日志)。
    扁平结构从根上避免了这个问题。
    """

    intent: str = Field(
        description="plan(信息够了,直接规划)/ clarify(缺关键信息,要追问)/ chat(闲聊或问一次性问题)"
    )
    reply: str = Field(default="", description="给用户看的一句自然中文")
    city: Optional[str] = Field(default=None, description="目的地城市")
    start_date: Optional[str] = Field(default=None, description="出发日期,格式 YYYY-MM-DD")
    end_date: Optional[str] = Field(default=None, description="返程日期,格式 YYYY-MM-DD")
    travel_days: Optional[int] = Field(default=None, description="旅行天数")
    transportation: Optional[str] = Field(default=None, description="交通方式")
    accommodation: Optional[str] = Field(default=None, description="住宿偏好")
    preferences: List[str] = Field(default_factory=list, description="偏好标签")
    free_text_input: str = Field(default="", description="其它明确要求")
    missing: List[str] = Field(default_factory=list, description="还缺哪些信息(中文名)")

    @field_validator("preferences", "missing", mode="before")
    @classmethod
    def _accept_string_lists(cls, value: Any) -> Any:
        """容忍模型把数组写成 ``"历史文化、美食"`` 这种字符串(实测会发生)。"""
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            for sep in ("、", ",", ";", "/", " "):
                if sep in text:
                    return [part.strip() for part in text.split(sep) if part.strip()]
            return [text]
        return value

    @field_validator("intent", mode="before")
    @classmethod
    def _normalize_intent(cls, value: Any) -> Any:
        """容忍大小写、空格和多余修饰(``"Plan"`` / ``"plan意图"``)。"""
        if isinstance(value, str):
            text = value.strip().lower()
            for name in ("plan", "clarify", "chat"):
                if name in text:
                    return name
        return value

    @field_validator("travel_days", mode="before")
    @classmethod
    def _coerce_days(cls, value: Any) -> Any:
        """把 ``"3 天"`` / ``"三天"`` / ``3.0`` 这类写法收敛成整数。

        这个字段声明的是 int,但模型实测会给字符串。**校验一失败,LangChain 就会
        让模型重试,而模型往往把同样的参数原样再发一遍,一路撞到 recursion_limit**
        —— 实测真的撞出过 ``GraphRecursionError``(见 AGENTS.md)。
        拿不准就返回 None,交给"信息不全 → 追问"那条路,比硬失败好。
        """
        if value is None or isinstance(value, int):
            return value
        if isinstance(value, float) and float(value).is_integer():
            return int(value)
        if isinstance(value, str):
            text = value.strip()
            digits = re.search(r"\d+", text)
            if digits:
                return int(digits.group())
            chinese = {
                "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
            }
            for char, number in chinese.items():
                if char in text:
                    return number
        return None


CHAT_SYSTEM_PROMPT = """你是「智能旅行规划助手」的对话入口。用户会用自然语言提需求,
你的任务是判断该做什么、把规划需要的信息抽出来,并给用户一句自然的回应。

**intent 只能取三个值:**
- `plan`:信息够了,可以直接生成行程(至少要有城市 + 一段明确时间)
- `clarify`:缺关键信息,需要追问(最常见的是没说什么时候去、去几天)
- `chat`:用户在闲聊、或问一次性问题(如"你能做什么""介绍一下你自己")

**范围约束(重要,必须遵守):**

1. **内容限定在中国大陆**。数据源是高德地图,只覆盖中国大陆;境外内容不要凭记忆推荐。
2. 用户**明确要求境外**时(如"去日本""推荐东京的景点"),如实说明只覆盖中国大陆,
   并问他要不要换成国内城市(这种情况 intent 用 `chat`)。
3. **「生成行程」必须先有用户给的明确城市**:`intent=plan` 的前提是用户自己说了城市,
   不能替他挑一个城市就闷头去跑规划。
   **但「只是聊天 / 让你推荐」不在此列** —— 没有城市也应该照常给建议,不许拿它当借口。
4. **不要把你的规则念给用户听**。"本服务的数据源只覆盖中国大陆""需要您指定一个城市"
   这类话只在用户确实问到境外、或确实要生成行程却缺城市时说**一次**,
   不要每轮重复,更不要在用户只是让你随便推荐时拿它当挡箭牌。

**用户把选择权交给你时(重要):**

用户说"随便""都行""你定""推荐一个就行"时,**不要再问同一个问题** ——
他那句话的意思就是"我不想选"。

这时应该直接给 **2~4 个具体选项**,一行一个,格式 `城市 · 景点 —— 一句话为什么`,
覆盖不同类型(历史 / 自然 / 美食 / 亲子),让他从里面挑。例如:

- 北京 · 故宫博物院 —— 想看明清皇宫和中轴线,历史感最强
- 西安 · 兵马俑 —— 想看震撼的考古现场
- 成都 · 大熊猫繁育研究基地 —— 带小孩或想轻松一点
- 杭州 · 西湖 —— 想慢逛、拍照、吃本地菜

给完选项后补一句"想排成完整行程的话,告诉我选哪个、大概什么时候去"即可。
**在这里重复追问是最糟的回应。**

**抽取规则:**
1. **你只负责"转述",不负责"算日期"** —— 这是硬性要求:
   - 用户说了**出发日期** → 填 `start_date`;
   - 用户说了**返程日期**(如"玩到 10 月 20 日""20 号回来")→ 填 `end_date`;
   - 用户说了**时长**(如"三天""一周")→ 只填 `travel_days`;
   - **永远不要自己推算返程日期**:`end_date` 只有在用户明确说了返程日时才有值。
     实测过模型把"三天"算成"两天",导致行程少一天 —— 这类推算全部交给后端做。
   - 但相对时间仍要换算成具体日期(用户消息里会给出"今天是 X",以此为基准):
     "下周末"、"国庆"、"后天" 这些属于**用户没说绝对日期**,需要你算。
2. **没提到的字段一律留空,不要编造**。交通、住宿留空时下游会兜底成
   "公共交通 / 经济型酒店"。
3. preferences 是偏好标签数组,例如 ["历史文化", "美食", "亲子"]。
4. 用户在补充信息时,结合对话历史把条件合并齐,别丢掉之前说过的。

**reply 字段**(用一句自然、简短的中文):
- intent=plan:复述你理解到的需求,例如"好的,北京 3 天,10-16 出发,我这就开始规划";
- intent=clarify:把缺的信息一次问清,别挤牙膏;
- intent=chat:直接回应。

**missing 字段**:intent=clarify 时列出还缺的信息(中文,如 "出发日期"、"行程天数")。
"""

_router_agent: Any = None


def _get_router_agent() -> Any:
    """意图识别用的 Agent(单例,惰性构建)。"""
    global _router_agent
    if _router_agent is None:
        _router_agent = create_agent(
            model=get_llm(),
            tools=[],
            system_prompt=CHAT_SYSTEM_PROMPT,
            response_format=ChatDecision,
            name="chat_router",
        )
    return _router_agent


def reset_router_agent() -> None:
    """重置单例(测试或重新配置时用)。"""
    global _router_agent
    _router_agent = None


def _format_context(message: str, history: Sequence[ChatMessage], today: date) -> str:
    """把"今天 + 对话历史 + 最新一句"拼成一次调用的输入。

    "今天"放在这里而不是系统提示词里,这样 Agent 可以复用(提示词不用每天重建)。
    """
    lines: List[str] = [f"【今天是 {today.isoformat()}】", ""]

    recent = list(history)[-MAX_HISTORY:]
    if recent:
        lines.append("【对话历史】")
        for item in recent:
            speaker = "用户" if item.role == "user" else "助手"
            lines.append(f"{speaker}:{item.content}")
        lines.append("")

    lines.append("【用户最新一句】")
    lines.append(message.strip() or "(空)")
    return "\n".join(lines)


def _parse_date(value: Optional[str]) -> Optional[date]:
    """尽量把模型给的日期字符串解析出来(兼容几种常见写法)。"""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def build_trip_request(decision: ChatDecision) -> Optional[TripRequest]:
    """把抽到的槽位拼成 ``TripRequest``;关键信息不全时返回 ``None``(交给追问)。

    这里**只做拼装和校验,不做猜测**:模型没给的日期不会替它编一个,
    宁可多问一轮。日期与天数可以互相补齐(给一段日期就算天数;给起始日+天数
    就推返程日),但两者都没有时只能追问。
    """
    city = (decision.city or "").strip()
    if not city:
        return None

    start = _parse_date(decision.start_date)
    end = _parse_date(decision.end_date)
    days = decision.travel_days

    if start and end:
        if end < start:
            start, end = end, start
        span = (end - start).days + 1
        if days and int(days) != span:
            # 模型既给了区间又给了天数、而且对不上。区间是"用户明确说的"所以以它为准,
            # 但这说明提示词可能又退化成"让模型算日期"了,值得看一眼日志。
            print(
                f"⚠️  天数({days})与日期区间({start}~{end} = {span} 天)不一致,以区间为准"
            )
        days = span
    elif start and days:
        end = start + timedelta(days=int(days) - 1)
    elif end and days:
        start = end - timedelta(days=int(days) - 1)
    else:
        return None

    if not start or not end or not days or days < 1:
        return None

    try:
        return TripRequest(
            city=city,
            start_date=start,
            end_date=end,
            travel_days=int(days),
            transportation=(decision.transportation or "").strip() or DEFAULT_TRANSPORTATION,
            accommodation=(decision.accommodation or "").strip() or DEFAULT_ACCOMMODATION,
            preferences=[str(p).strip() for p in (decision.preferences or []) if str(p).strip()],
            free_text_input=(decision.free_text_input or "").strip(),
        )
    except Exception as exc:  # noqa: BLE001 - 拼不出来就退回追问,别让请求 500
        print(f"⚠️  槽位拼不成 TripRequest: {type(exc).__name__}: {exc}")
        return None


async def decide(
    message: str, history: Sequence[ChatMessage], *, today: Optional[date] = None
) -> ChatDecision:
    """判断意图并抽取槽位(**一次**模型调用)。

    失败时**不抛异常**:退化成一次追问。理由见下面的 ``except`` ——
    意图识别挂掉不该让整轮对话变成一条报错,用户看到的应该是一句"我再问一次"。
    """
    agent = _get_router_agent()
    try:
        result = await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": _format_context(message, history, today or date.today()),
                    }
                ]
            },
            config={"recursion_limit": get_settings().agent_recursion_limit},
        )

        structured = result.get("structured_response")
        if isinstance(structured, ChatDecision):
            return structured
        if structured is not None:
            return ChatDecision.model_validate(structured)
        raise RuntimeError("意图识别没有返回结构化结果")
    except Exception as exc:  # noqa: BLE001
        # 结构化输出失败时,模型会把同样的参数重发,一路撞到 recursion_limit
        # (实测撞出过 GraphRecursionError)。这时候把错误抛给用户没有意义 ——
        # 他既看不懂,也没法处理。退化成追问,让他换个说法再说一遍。
        print(f"⚠️  意图识别失败({type(exc).__name__}: {exc}),退化成追问")
        return ChatDecision(
            intent="clarify",
            reply=(
                "抱歉,我刚才没太理解。你可以直接告诉我:想去哪个城市、大概什么时候出发、玩几天,"
                "比如「北京玩三天,下周末出发」。"
            ),
            missing=["目的地", "出发日期"],
        )


async def run_chat(request: ChatRequest) -> AsyncIterator[Dict[str, Any]]:
    """把一次聊天请求变成事件流。

    事件类型(与 ``/api/chat/stream`` 的 SSE 一一对应):

    - ``{"type": "message", "text": ...}``:给用户看的一句话
    - ``{"type": "progress", ...}``:规划过程中的节点进度(与 ``/plan/stream`` 同构)
    - ``{"type": "artifact", "kind": "trip_plan", ...}``:结构化行程,
      前端把它渲染成卡片(天气 / 景点 / 酒店 / 预算都在里面)
    - ``{"type": "done", "request": {...}}``:本轮结束
    """
    trip = request.trip

    if trip is None:
        decision = await decide(request.message, request.history)

        if decision.intent != "plan":
            if decision.reply:
                yield {"type": "message", "text": decision.reply}
            yield {"type": "done"}
            return

        trip = build_trip_request(decision)
        if trip is None:
            # 模型判定"可以规划"但槽位拼不出来,说明还差东西。
            # 这时候**不能**把它的 reply 也发出去 —— 那句多半是
            # "好的,我这就开始",紧接着又追问,用户会觉得系统在自说自话。
            missing = "、".join(x for x in (decision.missing or []) if x) or "出发日期"
            yield {"type": "message", "text": f"还差一点信息才能开始规划:{missing}。"}
            yield {"type": "done"}
            return

        if decision.reply:
            yield {"type": "message", "text": decision.reply}

    # ---- 到这里就复用现有流水线:检索(确定性)+ 规划(唯一那次模型调用) ----
    planner = get_trip_planner_agent()
    async for event in planner.plan_trip_stream(trip):
        event_type = event.get("type")
        if event_type == "progress":
            yield event
        elif event_type == "result":
            yield {
                "type": "artifact",
                "kind": "trip_plan",
                "degraded": event.get("degraded", False),
                "reason": event.get("reason", ""),
                "message": event.get("message", ""),
                "elapsed": event.get("elapsed"),
                "request": trip.model_dump(mode="json"),
                "data": event.get("data"),
            }
        else:
            yield event

    yield {"type": "done", "request": trip.model_dump(mode="json")}
