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

import json
import random
import re
from datetime import date, datetime, timedelta
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field, field_validator

from ..agents.trip_planner_agent import get_trip_planner_agent
from ..config import get_settings
from ..models.schemas import ChatMessage, ChatRequest, TripPlan, TripRequest
from .llm_service import get_llm
from .memory_store import (
    MemoryAccessError,
    SessionMemory,
    get_memory_store,
)

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
        description=(
            "plan(新行程,信息够了)/ refine(基于上一版行程修改)/ "
            "clarify(缺关键信息)/ chat(闲聊或一次性问题)"
        )
    )
    reply: str = Field(default="", description="给用户看的一句自然中文")
    city: Optional[str] = Field(default=None, description="目的地城市")
    start_date: Optional[str] = Field(default=None, description="出发日期,格式 YYYY-MM-DD")
    end_date: Optional[str] = Field(default=None, description="返程日期,格式 YYYY-MM-DD")
    travel_days: Optional[int] = Field(default=None, description="旅行天数")
    transportation: Optional[str] = Field(default=None, description="交通方式")
    accommodation: Optional[str] = Field(default=None, description="住宿偏好")
    preferences: List[str] = Field(default_factory=list, description="偏好标签")
    must_visit: List[str] = Field(
        default_factory=list,
        description=(
            "用户**明确点名**要去的景点/地点(比如从你推荐的选项里挑了「故宫博物院」)。"
            "这是硬约束,必须带下去"
        ),
    )
    free_text_input: str = Field(default="", description="其它明确要求")
    missing: List[str] = Field(default_factory=list, description="还缺哪些信息(中文名)")

    @field_validator("preferences", "missing", "must_visit", mode="before")
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
            for name in ("refine", "plan", "clarify", "chat"):
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

**intent 只能取四个值:**
- `plan`:信息够了,可以直接生成行程(至少要有城市 + 一段明确时间)
- `refine`:上下文里有「上一版行程」,用户是在修改它(如"第二天换成博物馆")
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

这时应该直接给 **2~4 个具体选项**,**每个占一行、以 `- ` 开头**,
格式 `城市 · 景点 —— 一句话为什么`,覆盖不同类型(历史 / 自然 / 美食 / 亲子)。
**不要把它们挤成一整段**,那样很难扫读。

**优先从下面这几条里挑**(它们是本次随机抽的,每次都不一样):

{examples}

如果它们跟用户这次的条件都不搭,可以再补充一个你觉得更合适的。
但**绝对不要每次都推荐同样的几个地方** —— 实测踩过这个坑:
格式示例被当成答案照抄,结果不管谁来问,推荐永远是那四个。

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
   - **用户完全没提时间时(既没有具体日期,也没有"下周末/国庆/三天后"这类说法),
     `start_date` / `end_date` 一律留空** —— 不要自己挑一个日期。
     实测踩过:用户只说"玩三天",模型自己编了个出发日,于是直接生成了一份
     用户从没同意过日期的行程。这种情况应该追问出发时间。
2. **没提到的字段一律留空,不要编造**。交通、住宿留空时下游会兜底成
   "公共交通 / 经济型酒店"。
3. preferences 是偏好标签数组,例如 ["历史文化", "美食", "亲子"]。
4. 用户在补充信息时,结合对话历史把条件合并齐,别丢掉之前说过的。
5. **用户点名要去的地方必须填进 `must_visit`**。典型场景:你上一轮推荐了几个景点,
   用户回了一句"就去故宫""第一个吧""兵马俑"—— 那就要把 **完整的名称**(如
   "故宫博物院")放进 `must_visit`,不能只留在对话里。这一条**不能漏**:
   漏了的话下游检索不到它,规划就会把用户选的景点当成普通候选、甚至排掉。
   注意区分:`must_visit` 是"一定要去的具体地点",`preferences` 只是"偏好类型"。

**会话内修改(只在出现了「上一版行程」时使用):**

用户说"第二天换成博物馆""酒店换近一点""再加一个晚上"这类话时:
1. `intent` 填 `refine`,不要重新追问城市、日期、天数 —— 上下文里已经有。
2. **只抽取本轮明确改变的内容**,不要把上一版行程里的旧槽位当成用户这一轮重新说的话。
3. 把具体修改写进 `free_text_input`,尽量保留用户的原始措辞。
4. 如果用户明确改了城市、日期或天数,照常填对应槽位;没改的槽位留空。
5. `reply` 简短复述这次要改什么,例如"好的,把第二天换成博物馆,其他安排保持不变"。

**没有「上一版行程」时绝对不能返回 `refine`**。

**reply 字段**(用一句自然、简短的中文):
- intent=plan:复述你理解到的需求,例如"好的,北京 3 天,10-16 出发,我这就开始规划";
- intent=refine:复述这次要改什么,并说明其余部分保持;
- intent=clarify:把缺的信息一次问清,别挤牙膏;
- intent=chat:直接回应。

**missing 字段**:intent=clarify 时列出还缺的信息(中文,如 "出发日期"、"行程天数")。
"""

# 随机示例池:每次抽几条塞进提示词当"格式示例"。
#
# 为什么需要它:光写"不要照抄示例"是没用的 —— 模型会盯着示例。
# 让**示例本身每次都变**,照抄也就不会重复了。
# 全部限定在中国大陆,并且刻意混入一些不那么烂大街的地方,
# 免得推荐永远绕着"故宫 / 兵马俑 / 西湖"打转。
RECOMMENDATION_POOL: List[tuple] = [
    ("北京", "故宫博物院", "想看明清皇宫和中轴线"),
    ("西安", "秦始皇兵马俑", "想看震撼的考古现场"),
    ("成都", "大熊猫繁育研究基地", "带小孩,或想轻松一点"),
    ("杭州", "西湖", "想慢逛、拍照、吃本地菜"),
    ("泉州", "开元寺", "想看海丝古迹又不想人挤人"),
    ("大同", "云冈石窟", "想看北魏石窟造像"),
    ("景德镇", "陶溪川", "想逛陶瓷市集、动手做点东西"),
    ("洛阳", "龙门石窟", "想看石刻,顺带看牡丹"),
    ("青岛", "八大关", "想吹海风、看老建筑"),
    ("重庆", "洪崖洞", "想吃辣、看立体城市"),
    ("苏州", "拙政园", "想看园林、听评弹"),
    ("敦煌", "莫高窟", "想看壁画和沙漠"),
    ("厦门", "鼓浪屿", "想在海边发呆"),
    ("哈尔滨", "中央大街", "想看冰雪、吃俄式西餐"),
    ("长沙", "岳麓山", "想吃夜宵顺便爬山"),
    ("丽江", "束河古镇", "想慢下来住几天"),
]

# 每次抽几条当示例
RECOMMENDATION_SAMPLE_SIZE = 3


def build_system_prompt() -> str:
    """渲染系统提示词,并随机换掉里面那几条"格式示例"。

    用 ``replace`` 而不是 ``str.format``:提示词里有反引号、中文括号,
    以后万一出现 ``{`` / ``}`` 也不会被当成占位符。
    """
    sample = random.sample(RECOMMENDATION_POOL, RECOMMENDATION_SAMPLE_SIZE)
    examples = "\n".join(f"- {city} · {spot} —— {why}" for city, spot, why in sample)
    return CHAT_SYSTEM_PROMPT.replace("{examples}", examples)

# 意图识别用的模型:直接绑 ChatDecision 这个"工具"。
#
# 为什么不用 create_agent(和规划那一步不一样):
#   1. 我们要的是**逐字流式**回话,而 create_agent 只在最后给完整结果;
#   2. 更要紧的是,create_agent 在结构化输出校验失败时会**让模型重试**,
#      而模型往往把同样的参数原样再发一遍,一路撞到 recursion_limit
#      (实测撞出过 GraphRecursionError)。这里我们只调一次、自己解析、
#      失败就退化成追问 —— 没有循环,也就没有撞上限这回事。
_router_model: Any = None


def _get_router_model() -> Any:
    global _router_model
    if _router_model is None:
        _router_model = get_llm().bind_tools([ChatDecision], tool_choice="required")
    return _router_model


def reset_router_agent() -> None:
    """重置单例(测试或重新配置时用)。"""
    global _router_model
    _router_model = None


# 从**还没写完**的 JSON 片段里抠出 reply 的当前值。
# 模型的工具参数是按字段顺序吐的,而 reply 是第二个字段 —— 所以它出现得很早。
_REPLY_PATTERN = re.compile(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)')


def _decode_partial_string(fragment: str) -> str:
    """把(可能被截断的)JSON 字符串片段解码成明文。

    截断点可能正好落在半个转义序列上(比如末尾是 ``\\`` 或 ``\\u12``),
    那就去掉最后 1~2 个字符再试。
    """
    for cut in range(3):
        candidate = fragment if cut == 0 else fragment[:-cut]
        try:
            return json.loads('"' + candidate + '"')
        except Exception:  # noqa: BLE001 - 截断导致的解析失败是常态
            continue
    return fragment


def extract_reply(raw_args: str) -> str:
    """取出 ``reply`` 字段到目前为止的值(可能还没写完)。"""
    match = _REPLY_PATTERN.search(raw_args)
    return _decode_partial_string(match.group(1)) if match else ""


def _normalize_escapes(text: str) -> str:
    """把**字面量的** ``\\n`` / ``\\t`` 还原成真正的换行和制表符。

    实测踩过:模型有时会把换行写成"双重转义"(JSON 里是 ``\\\\n``),
    解出来就是两个字符 ``\\`` + ``n``,用户看到的是字面的 ``\\n``。
    三次请求里出现了一次 —— 旅行对话里没人会真的想显示一个反斜杠加 n,
    所以这里直接还原。
    """
    if "\\n" not in text and "\\t" not in text:
        return text
    return text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")


_FALLBACK_REPLY = (
    "抱歉,我刚才没太理解。你可以直接告诉我:想去哪个城市、大概什么时候出发、玩几天,"
    "比如「北京玩三天,下周末出发」。"
)


def _fallback_decision() -> ChatDecision:
    return ChatDecision(intent="clarify", reply=_FALLBACK_REPLY, missing=["目的地", "出发日期"])


def _format_session_context(session: SessionMemory) -> str:
    """把上一版请求和行程压缩成意图识别可读的上下文。"""
    request = TripRequest.model_validate(session.request)
    plan = TripPlan.model_validate(session.plan)
    lines = [
        "【上一版行程】",
        f"- 城市:{plan.city}",
        f"- 日期:{plan.start_date} 至 {plan.end_date}",
        f"- 交通:{request.transportation};住宿:{request.accommodation}",
        f"- 偏好:{', '.join(request.preferences) if request.preferences else '无'}",
    ]
    for day in plan.days:
        attractions = "、".join(item.name for item in day.attractions) or "无"
        hotel = day.hotel.name if day.hotel else "无"
        lines.append(
            f"- 第{day.day_index + 1}天({day.date}):景点 {attractions};酒店 {hotel}"
        )
    return "\n".join(lines)


def _format_context(
    message: str,
    history: Sequence[ChatMessage],
    today: date,
    *,
    session: Optional[SessionMemory] = None,
) -> str:
    """把"今天 + 上一版行程 + 对话历史 + 最新一句"拼成一次调用的输入。

    "今天"放在这里而不是系统提示词里,这样 Agent 可以复用(提示词不用每天重建)。
    """
    lines: List[str] = [f"【今天是 {today.isoformat()}】", ""]

    if session is not None:
        lines.append(_format_session_context(session))
        lines.append("")

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


def build_trip_request(
    decision: ChatDecision,
    *,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[TripRequest]:
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
            must_visit=[str(p).strip() for p in (decision.must_visit or []) if str(p).strip()],
            free_text_input=(decision.free_text_input or "").strip(),
            session_id=(session_id or "").strip() or None,
            user_id=(user_id or "").strip() or None,
        )
    except Exception as exc:  # noqa: BLE001 - 拼不出来就退回追问,别让请求 500
        print(f"⚠️  槽位拼不成 TripRequest: {type(exc).__name__}: {exc}")
        return None


def _dedupe(values: Sequence[str]) -> List[str]:
    """去空、去重,同时保留原顺序。"""
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def merge_refinement(
    previous: TripRequest,
    decision: ChatDecision,
    message: str,
    *,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[TripRequest]:
    """把本轮修改确定性合并到上一版请求上。

    模型只负责说"这轮改什么",已有城市/日期由代码保留,避免模型漏抄一个槽位就把
    整段上下文丢掉。上一版稳定请求单独存在会话表里,因此多轮修改不会让
    ``free_text_input`` 无限累积旧指令。
    """
    city = (decision.city or previous.city or "").strip()
    if not city:
        return None

    start = _parse_date(decision.start_date) or previous.start_date
    end_input = _parse_date(decision.end_date)
    days_input = decision.travel_days

    if end_input is not None:
        end = end_input
    elif days_input is not None:
        end = start + timedelta(days=int(days_input) - 1)
    elif _parse_date(decision.start_date) is not None:
        end = start + timedelta(days=previous.travel_days - 1)
    else:
        end = previous.end_date

    if end < start:
        return None

    edit = (decision.free_text_input or message or "").strip()
    preferences = _dedupe([*previous.preferences, *decision.preferences])
    must_visit = _dedupe([*previous.must_visit, *decision.must_visit])

    try:
        return TripRequest(
            city=city,
            start_date=start,
            end_date=end,
            travel_days=(end - start).days + 1,
            transportation=(decision.transportation or previous.transportation).strip(),
            accommodation=(decision.accommodation or previous.accommodation).strip(),
            preferences=preferences,
            must_visit=must_visit,
            free_text_input=edit,
            session_id=(session_id or "").strip() or None,
            user_id=(user_id or "").strip() or None,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  修改要求合并不出 TripRequest: {type(exc).__name__}: {exc}")
        return None


def _with_identity(
    trip: TripRequest, *, session_id: Optional[str], user_id: Optional[str]
) -> TripRequest:
    """把 ChatRequest 上的会话身份补到结构化 trip 上。"""
    return trip.model_copy(
        update={
            "session_id": (session_id or trip.session_id or "").strip() or None,
            "user_id": (user_id or trip.user_id or "").strip() or None,
        }
    )


async def decide_streaming(
    message: str,
    history: Sequence[ChatMessage],
    *,
    today: Optional[date] = None,
    model: Any = None,
    session: Optional[SessionMemory] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """判断意图、抽取槽位(**一次**模型调用),同时把回复逐字流出去。

    产出两类事件(前端把 ``delta`` 追加到当前助手气泡里):

    - ``{"delta": "北京三天…"}`` —— 助手回复的增量,可能有很多条;
    - ``{"decision": ChatDecision, "streamed": "已经流出去的全文"}`` —— 恰好一条,在最后。

    失败**不抛异常**:退化成一次追问,并把兜底话术也当增量吐出去,
    免得用户看到半句话就没下文了。
    """
    router = model or _get_router_model()
    payload = [
        {"role": "system", "content": build_system_prompt()},
        {
            "role": "user",
            "content": _format_context(
                message, history, today or date.today(), session=session
            ),
        },
    ]

    raw_args = ""
    streamed = ""

    try:
        async for chunk in router.astream(payload):
            for piece in getattr(chunk, "tool_call_chunks", None) or []:
                fragment = piece.get("args") or ""
                if not fragment:
                    continue
                raw_args += fragment
                current = _normalize_escapes(extract_reply(raw_args))
                # 末尾若是反斜杠,它可能只是**半个转义序列**(比如刚要吐 \n 里的那个 \),
                # 先扣住不发 —— 否则等真正的换行到达时,文本就不再以它为前缀,
                # 前缀校验会把后面所有内容挡住,用户看到的是半句话。
                # (实测踩过:回复被截成"…推荐："后面跟一个孤零零的反斜杠。)
                safe = current[:-1] if current.endswith("\\") else current
                if safe.startswith(streamed) and len(safe) > len(streamed):
                    yield {"delta": safe[len(streamed) :]}
                    streamed = safe
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  意图识别流式中断({type(exc).__name__}: {exc})")

    decision: Optional[ChatDecision] = None
    if raw_args:
        try:
            decision = ChatDecision.model_validate(json.loads(raw_args))
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️  意图识别结果解析失败({type(exc).__name__}: {exc}),退化成追问")

    if decision is None:
        decision = _fallback_decision()

    # 把"最终决定要说的"和"已经流出去的"对齐:少了的补上,不看半句、也不重复。
    reply = _normalize_escapes(decision.reply or "")
    common = 0
    for said, want in zip(streamed, reply):
        if said != want:
            break
        common += 1

    if common < len(streamed):
        # 已经发出去的内容撤不回来。与其让用户看半句话,不如把剩下的续上 ——
        # 顶多中间一两个字对不齐,但整句是完整的。
        print(
            f"⚠️  流出去的内容与最终 reply 有 {len(streamed) - common} 字对不上,已按公共前缀续上"
        )
    if common < len(reply):
        yield {"delta": reply[common:]}

    yield {"decision": decision, "streamed": reply}


async def decide(
    message: str,
    history: Sequence[ChatMessage],
    *,
    today: Optional[date] = None,
    session: Optional[SessionMemory] = None,
) -> ChatDecision:
    """只要判断结果、不要流式时的便捷封装(测试和内部调用用)。"""
    decision = _fallback_decision()
    async for item in decide_streaming(message, history, today=today, session=session):
        if "decision" in item:
            decision = item["decision"]
    return decision


async def run_chat(request: ChatRequest) -> AsyncIterator[Dict[str, Any]]:
    """把一次聊天请求变成事件流。

    事件类型(与 ``/api/chat/stream`` 的 SSE 一一对应):

    - ``{"type": "message", "text": ...}``:给用户看的一句话
    - ``{"type": "progress", ...}``:规划过程中的节点进度(与 ``/plan/stream`` 同构)
    - ``{"type": "artifact", "kind": "trip_plan", ...}``:结构化行程,
      前端把它渲染成卡片(天气 / 景点 / 酒店 / 预算都在里面)
    - ``{"type": "done", "request": {...}}``:本轮结束
    """
    session_id = (
        request.session_id or (request.trip.session_id if request.trip else None) or ""
    ).strip()
    user_id = (
        request.user_id or (request.trip.user_id if request.trip else None) or ""
    ).strip()

    previous_memory: Optional[SessionMemory] = None
    if session_id:
        try:
            previous_memory = get_memory_store().get_session(
                session_id, user_id=user_id or None
            )
        except MemoryAccessError as exc:
            raise RuntimeError(str(exc)) from exc

    trip = _with_identity(request.trip, session_id=session_id, user_id=user_id) if request.trip else None
    previous_plan: Optional[TripPlan] = None
    is_refine = False

    if trip is None:
        decision: Optional[ChatDecision] = None

        # 助手这次要说的话是**边生成边流出去的** —— 每个 message 事件是一段增量,
        # 前端负责追加。虽然流式不好再"撤回",但用户能看到字在往外冒,
        # 而不是盯着空气泡等十几秒。
        async for item in decide_streaming(
            request.message, request.history, session=previous_memory
        ):
            if "delta" in item:
                yield {"type": "message", "text": item["delta"]}
            else:
                decision = item["decision"]

        if decision is None:  # 防御:decide_streaming 一定会给一条 decision
            decision = _fallback_decision()
            yield {"type": "message", "text": decision.reply}

        if decision.intent == "refine":
            if previous_memory is None:
                yield {
                    "type": "message",
                    "text": "\n\n我没有找到可修改的上一版行程。请告诉我城市、出发时间和天数,我重新规划。",
                }
                yield {"type": "done", "session_id": session_id or None}
                return

            previous_request = TripRequest.model_validate(previous_memory.request)
            trip = merge_refinement(
                previous_request,
                decision,
                request.message,
                session_id=session_id,
                user_id=user_id,
            )
            if trip is None:
                yield {
                    "type": "message",
                    "text": "\n\n这次修改我没有理解清楚,请再说得具体一点,例如「把第二天换成博物馆」。",
                }
                yield {"type": "done", "session_id": session_id or None}
                return
            previous_plan = TripPlan.model_validate(previous_memory.plan)
            is_refine = True
        elif decision.intent != "plan":
            yield {"type": "done"}
            return

        if not is_refine:
            trip = build_trip_request(
                decision, session_id=session_id or None, user_id=user_id or None
            )
        if trip is None:
            # 模型判定"可以规划"但槽位拼不出来。这时候那句话**已经流出去了**
            # (多半是"好的,我这就开始"),所以只能顺着往下补一句,把话收回圆。
            missing = "、".join(x for x in (decision.missing or []) if x) or "出发日期"
            yield {"type": "message", "text": f"\n\n不过还差一点信息:{missing}。告诉我我就开始。"}
            yield {"type": "done"}
            return

    # ---- 到这里就复用现有流水线:检索(确定性)+ 规划(唯一那次模型调用) ----
    planner = get_trip_planner_agent()
    stream = (
        planner.plan_trip_stream(trip, previous_plan=previous_plan)
        if previous_plan is not None
        else planner.plan_trip_stream(trip)
    )
    memory_saved = False
    memory_version: Optional[int] = None

    async for event in stream:
        event_type = event.get("type")
        if event_type == "progress":
            yield event
        elif event_type == "result":
            if (
                session_id
                and not event.get("degraded", False)
                and event.get("data")
            ):
                try:
                    plan = TripPlan.model_validate(event["data"])
                    saved = get_memory_store().save_successful_plan(
                        session_id=session_id,
                        user_id=user_id or None,
                        request=trip,
                        plan=plan,
                        last_message=request.message,
                        reset_base=not is_refine,
                    )
                    memory_saved = True
                    memory_version = saved.version
                except MemoryAccessError as exc:
                    raise RuntimeError(str(exc)) from exc
                except Exception as exc:  # noqa: BLE001
                    # 记忆写入失败不能吞掉已经生成好的行程,但要明确告诉客户端没保存。
                    print(f"⚠️  会话记忆写入失败: {type(exc).__name__}: {exc}")

            yield {
                "type": "artifact",
                "kind": "trip_plan",
                "degraded": event.get("degraded", False),
                "reason": event.get("reason", ""),
                "message": event.get("message", ""),
                "elapsed": event.get("elapsed"),
                "request": trip.model_dump(mode="json"),
                "data": event.get("data"),
                "memory_saved": memory_saved,
            }
        else:
            yield event

    yield {
        "type": "done",
        "request": trip.model_dump(mode="json"),
        "session_id": session_id or None,
        "memory_saved": memory_saved,
        "memory_version": memory_version,
    }
