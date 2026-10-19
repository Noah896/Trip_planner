"""把用户明确表达的偏好转换为长期记忆,并在后续规划中注入。

这里刻意不使用模型:从 ``preferences`` 和少量稳定的自由文本规则中抽取,
不新增调用,也不会从已生成行程里反推偏好。当前请求中的显式值优先于历史记忆。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..models.schemas import TripRequest
from .memory_store import (
    HistoryMemory,
    MemoryFact,
    MemoryStore,
    get_memory_store,
)


@dataclass(frozen=True)
class ExplicitPreference:
    """一条从本轮用户输入中确定性抽出的偏好。"""

    category: str
    key: str
    value: str
    source_text: str


@dataclass(frozen=True)
class _PreferenceRule:
    category: str
    key: str
    value: str
    phrases: Tuple[str, ...]


@dataclass(frozen=True)
class MemoryApplication:
    """记忆注入后的规划请求,以及本轮真正用到的历史事实。"""

    request: TripRequest
    used_facts: List[MemoryFact]


@dataclass(frozen=True)
class HistoryRecall:
    """一次历史行程召回的结果。"""

    requested: bool
    selected: Optional[HistoryMemory]
    candidates: List[HistoryMemory]


_WHITESPACE = re.compile(r"\s+")
_MAX_FACT_TEXT = 80
_HISTORY_REFERENCE_PHRASES = (
    "照上次",
    "按照上次",
    "像上次",
    "和上次一样",
    "跟上次一样",
    "上次去",
    "上一次",
    "上次",
    "之前去",
    "那次",
    "那次行程",
    "再来一份",
    "再去一次",
)

# 只收录具有跨行程复用价值、且用户说法足够明确的规则。
# 遍历顺序有意义:"不吃辣"必须排在"吃辣"前面。
_PREFERENCE_RULES: Tuple[_PreferenceRule, ...] = (
    _PreferenceRule(
        "diet", "spicy", "不吃辣", ("不吃辣", "不能吃辣", "不要辣", "忌辣", "怕辣")
    ),
    _PreferenceRule("diet", "spicy", "吃辣", ("吃辣", "喜欢辣", "无辣不欢")),
    _PreferenceRule(
        "diet", "seafood", "不吃海鲜", ("不吃海鲜", "海鲜过敏", "忌海鲜")
    ),
    _PreferenceRule("diet", "vegetarian", "素食", ("素食", "吃素")),
    _PreferenceRule("diet", "halal", "清真饮食", ("清真", "穆斯林饮食")),
    _PreferenceRule(
        "travel", "with_children", "带小孩", ("带孩子", "带小孩", "亲子", "小朋友")
    ),
    _PreferenceRule(
        "travel", "with_elderly", "带老人", ("带老人", "带父母", "带长辈")
    ),
    _PreferenceRule(
        "accessibility",
        "mobility",
        "行动不便",
        ("行动不便", "需要无障碍", "坐轮椅", "轮椅出行"),
    ),
    _PreferenceRule(
        "pace", "relaxed", "节奏轻松", ("慢节奏", "轻松一点", "不想太累", "休闲游")
    ),
    _PreferenceRule(
        "pace", "intensive", "行程紧凑", ("特种兵", "打卡多", "行程紧凑")
    ),
)


def _normalize_text(value: Any) -> str:
    text = _WHITESPACE.sub(" ", str(value or "")).strip()
    return text[:_MAX_FACT_TEXT]


def _match_rule(text: str) -> Optional[ExplicitPreference]:
    for rule in _PREFERENCE_RULES:
        if any(phrase in text for phrase in rule.phrases):
            return ExplicitPreference(
                category=rule.category,
                key=rule.key,
                value=rule.value,
                source_text=text,
            )
    return None


def _history_reference_positions(text: str) -> List[int]:
    positions: List[int] = []
    for phrase in _HISTORY_REFERENCE_PHRASES:
        start = 0
        while True:
            index = text.find(phrase, start)
            if index < 0:
                break
            positions.append(index)
            start = index + len(phrase)
    return positions


def recall_trip_history(
    user_id: Optional[str],
    texts: List[str],
    *,
    store: Optional[MemoryStore] = None,
    limit: int = 10,
) -> HistoryRecall:
    """识别"上次/之前"类表述,并确定性地选出最匹配的历史行程。

    用户点名城市时,按城市与"上次"等词的距离选最近的一次;否则取最新成功行程。
    不调用模型,也不做向量相似度。
    """
    normalized_texts = []
    for text in texts:
        normalized = _normalize_text(text)
        if normalized:
            normalized_texts.append(normalized)
    combined = "\n".join(normalized_texts)
    reference_positions = _history_reference_positions(combined)
    if not reference_positions:
        return HistoryRecall(requested=False, selected=None, candidates=[])

    owner = (user_id or "").strip()
    if not owner:
        return HistoryRecall(requested=True, selected=None, candidates=[])

    memory_store = store or get_memory_store()
    histories = memory_store.get_trip_history(owner, limit=limit)
    if not histories:
        return HistoryRecall(requested=True, selected=None, candidates=[])

    city_scores = []
    for history in histories:
        city = (history.city or "").strip()
        if not city:
            continue
        city_positions = []
        start = 0
        while True:
            index = combined.find(city, start)
            if index < 0:
                break
            city_positions.append(index)
            start = index + len(city)
        if city_positions:
            distance = min(
                abs(city_pos - reference_pos)
                for city_pos in city_positions
                for reference_pos in reference_positions
            )
            city_scores.append((distance, history))

    if city_scores:
        best_distance = min(score for score, _ in city_scores)
        matching_ids = {
            history.id for score, history in city_scores if score == best_distance
        }
        ordered = [
            history for history in histories if history.id in matching_ids
        ] + [
            history for history in histories if history.id not in matching_ids
        ]
    else:
        ordered = list(histories)

    return HistoryRecall(
        requested=True,
        selected=ordered[0] if ordered else None,
        candidates=ordered[:3],
    )


def extract_explicit_preferences(request: TripRequest) -> List[ExplicitPreference]:
    """从本轮请求抽取稳定的显式偏好,不猜测隐含意图。

    ``preferences`` 里的每一项都视为用户显式表达;已知短语会归一到稳定键,
    其它标签按普通旅行偏好保存。自由文本只匹配少量高置信规则,避免把
    "第二天换成博物馆" 这类一次性修改误记成长期偏好。
    """
    facts: Dict[Tuple[str, str], ExplicitPreference] = {}

    for raw in request.preferences:
        text = _normalize_text(raw)
        if not text:
            continue
        fact = _match_rule(text) or ExplicitPreference(
            category="preference",
            key=f"pref:{text.casefold()}",
            value=text,
            source_text=text,
        )
        facts[(fact.category, fact.key)] = fact

    free_text = _normalize_text(request.free_text_input)
    if free_text:
        free_text_keys = set()
        for rule in _PREFERENCE_RULES:
            if any(phrase in free_text for phrase in rule.phrases):
                key = (rule.category, rule.key)
                # 规则顺序即优先级:"不吃辣"必须压住子串"吃辣"。
                if key in free_text_keys:
                    continue
                fact = ExplicitPreference(
                    category=rule.category,
                    key=rule.key,
                    value=rule.value,
                    source_text=free_text,
                )
                # 自由文本通常更具体,覆盖 preferences 里的泛化标签。
                facts[key] = fact
                free_text_keys.add(key)

    return list(facts.values())


def _dedupe_preferences(values: List[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for raw in values:
        text = _normalize_text(raw)
        normalized = text.casefold()
        if text and normalized not in seen:
            seen.add(normalized)
            result.append(text)
    return result


def apply_long_term_memory(
    request: TripRequest,
    *,
    store: Optional[MemoryStore] = None,
) -> MemoryApplication:
    """把用户 active 偏好合并进本次规划请求。

    当前请求里已经显式表达的同一 ``(category, key)`` 优先,历史值不会覆盖它。
    返回的 ``used_facts`` 只包含真正追加到请求里的历史事实。
    """
    user_id = (request.user_id or "").strip()
    if not user_id:
        return MemoryApplication(request=request, used_facts=[])

    memory_store = store or get_memory_store()
    current_keys = {
        (fact.category, fact.key) for fact in extract_explicit_preferences(request)
    }
    preferences = list(request.preferences)
    used_facts: List[MemoryFact] = []

    for fact in memory_store.get_facts(user_id):
        if (fact.category, fact.key) in current_keys:
            continue
        if not isinstance(fact.value, str):
            continue
        value = _normalize_text(fact.value)
        if not value:
            continue
        before = {item.casefold() for item in preferences}
        if value.casefold() in before:
            continue
        preferences.append(value)
        used_facts.append(fact)

    if not used_facts:
        return MemoryApplication(request=request, used_facts=[])

    enriched = request.model_copy(
        update={"preferences": _dedupe_preferences(preferences)}
    )
    return MemoryApplication(request=enriched, used_facts=used_facts)


def save_explicit_preferences(
    request: TripRequest,
    *,
    store: Optional[MemoryStore] = None,
) -> List[MemoryFact]:
    """保存本轮显式偏好。调用方必须只在实际规划成功后调用。"""
    user_id = (request.user_id or "").strip()
    if not user_id:
        return []

    memory_store = store or get_memory_store()
    saved: List[MemoryFact] = []
    for fact in extract_explicit_preferences(request):
        saved.append(
            memory_store.upsert_fact(
                user_id=user_id,
                category=fact.category,
                key=fact.key,
                value=fact.value,
                source_text=fact.source_text,
                source_session_id=request.session_id,
            )
        )
    return saved


def used_memory_payload(facts: List[MemoryFact]) -> List[Dict[str, Any]]:
    """生成 API 与 SSE 共用的精简记忆回执。"""
    return [
        {
            "category": fact.category,
            "key": fact.key,
            "value": str(fact.value),
            "source_text": fact.source_text,
        }
        for fact in facts
    ]


def used_history_payload(history: Optional[HistoryMemory]) -> Optional[Dict[str, Any]]:
    """生成聊天回执里的历史行程摘要。"""
    if history is None:
        return None
    request = history.request
    return {
        "history_id": history.id,
        "city": history.city,
        "start_date": history.start_date,
        "end_date": history.end_date,
        "travel_days": int(request.get("travel_days") or 0),
        "preferences": list(request.get("preferences") or []),
    }
