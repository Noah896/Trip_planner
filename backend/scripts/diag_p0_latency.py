#!/usr/bin/env python
"""P0 隔离诊断:定位 ``/api/trip/plan`` 的耗时瓶颈到底在哪一段。

背景
----
``app/services/amap_mcp.py`` 改成常驻 streamable-http 之后,单次工具调用已经
降到 ~1.3s,但端到端 ``/plan`` 依然 11 分钟不返回(客户端先超时)。剩下的怀疑
对象是「模型侧延迟」和「检索 Agent 自己反复调工具的循环」。本脚本把链路拆开
单独计时,而不是一遍遍跑完整请求 —— 后者一次要打 4 个 Agent,既慢又费额度。

**`tool` / `detail` / `retrieval` 三个阶段完全不调用大模型,零额度。**
`planner` / `graph` 会真实调用模型:三路检索已经改成确定性服务层调用,
所以 `graph` 现在也只要 1~2 次模型调用。

用法
----
在任意目录下执行(脚本会先把工作目录切到 ``backend/``,否则 ``.env`` 读不到)::

    & 'E:\\hello agents_demo\\.venv\\Scripts\\python.exe' -X utf8 scripts\\diag_p0_latency.py

常用变体::

    # 只测工具链路,一次模型调用都不花
    ... --stages tool

    # 只跑三路检索(确定性调用,零额度)
    ... --stages tool,detail,retrieval

    # 真实全链路(费用最高,注意 AGENTS.md 里的额度提醒)
    ... --stages graph

输出
----
每个阶段一段:墙钟耗时 / LLM 调用次数与逐次耗时 / MCP 工具调用次数与逐次耗时 /
最终文本长度。超时的阶段会被 ``asyncio.timeout`` 主动中断并明确标记,不会像
之前那样把 900s 客户端超时打满。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---- 必须在 import app.* 之前 ----
# app.config 顶部的 load_dotenv() 读的是「当前工作目录」,从别的目录启动脚本会
# 静默读不到 .env(表现为 "LLM API Key 未配置"),所以先切到 backend/。
BACKEND_ROOT = Path(__file__).resolve().parents[1]
os.chdir(BACKEND_ROOT)
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from langchain.agents import create_agent  # noqa: E402

import app.services.amap_mcp as amap_mcp  # noqa: E402
from app.agents.trip_planner_agent import (  # noqa: E402
    PLANNER_AGENT_PROMPT,
    MultiAgentTripPlanner,
)
from app.models.schemas import TripPlan, TripRequest  # noqa: E402
from app.services.amap_mcp import (  # noqa: E402
    ensure_amap_server,
    find_tool,
    get_amap_tools,
    shutdown_amap_server,
)
from app.services.llm_service import get_llm  # noqa: E402
from app.services.trip_retrieval import (  # noqa: E402
    retrieve_attractions,
    retrieve_hotels,
    retrieve_weather,
)

# ============ 默认测试数据 ============

DEFAULT_CITY = "北京"
DEFAULT_KEYWORDS = "历史文化"
DEFAULT_PREFERENCES = ["历史文化", "美食"]

# 规划 Agent 不需要联网,喂固定输入即可单独计时。
CANNED_ATTRACTIONS = """1. 故宫博物院 / 北京市东城区景山前街4号 / 116.397029,39.917839 / 历史文化 / 180分钟 / 60元 / 明清两代皇宫,中轴线核心
2. 天坛公园 / 北京市东城区天坛东里甲1号 / 116.412100,39.882000 / 历史文化 / 120分钟 / 15元 / 明代皇家祭天场所
3. 颐和园 / 北京市海淀区新建宫门路19号 / 116.275000,39.999000 / 园林 / 150分钟 / 30元 / 清代皇家园林,昆明湖与万寿山"""

CANNED_WEATHER = """2025-06-01 / 晴 / 多云 / 28 / 18 / 南风 / 1-3级
2025-06-02 / 多云 / 阴 / 26 / 17 / 东南风 / 1-3级"""

CANNED_HOTELS = """1. 北京王府井希尔顿酒店 / 北京市东城区王府井东街8号 / 116.410000,39.915000 / 豪华型 / 900-1200元 / 4.6 / 适合商务与家庭
2. 北京前门建国饭店 / 北京市西城区永安路175号 / 116.390000,39.895000 / 舒适型 / 500-700元 / 4.3 / 适合观光客,近天坛
3. 如家酒店(北京前门大栅栏店) / 北京市西城区大栅栏西街 / 116.395000,39.893000 / 经济型 / 250-350元 / 4.1 / 适合预算有限的游客"""


# ============ 计时数据结构 ============


@dataclass
class Step:
    """一次 LLM 调用或一次工具调用。"""

    kind: str  # "llm" | "tool" | "retrieve"
    name: str
    start: float  # 相对阶段起点的偏移(秒)
    duration: float
    note: str = ""


@dataclass
class Stage:
    """一个被单独计时的阶段。"""

    label: str
    timeout: float = 0.0
    wall: float = 0.0
    timed_out: bool = False
    error: str = ""
    steps: List[Step] = field(default_factory=list)
    text: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
    messages: List[Any] = field(default_factory=list)

    @property
    def llm_steps(self) -> List[Step]:
        return [s for s in self.steps if s.kind == "llm"]

    @property
    def tool_steps(self) -> List[Step]:
        return [s for s in self.steps if s.kind == "tool"]

    @property
    def retrieval_steps(self) -> List[Step]:
        return [s for s in self.steps if s.kind == "retrieve"]

    @property
    def llm_total(self) -> float:
        return sum(s.duration for s in self.llm_steps)

    @property
    def tool_total(self) -> float:
        return sum(s.duration for s in self.tool_steps)

    @property
    def retrieval_total(self) -> float:
        return sum(s.duration for s in self.retrieval_steps)


# ============ 小工具 ============


def _short(text: Any, limit: int) -> str:
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + f"…(+{len(collapsed) - limit})"


def _message_text(message: Any) -> str:
    """把消息内容归一化成文本(与 trip_planner_agent._last_message_text 同义)。"""
    content = getattr(message, "content", message)
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(block.get("text", "") or "")
        return "\n".join(p for p in parts if p)
    return content or ""


def _extract_message(output: Any) -> Any:
    """``on_chat_model_end`` 的 output 可能是 ChatResult,统一取回消息对象。"""
    generations = getattr(output, "generations", None)
    if generations:
        with contextlib.suppress(Exception):
            return generations[0][0].message
    return output


def _llm_note(output: Any) -> str:
    """从 ``on_chat_model_end`` 的 output 里挤出有用的信息。"""
    message = _extract_message(output)
    parts: List[str] = []

    tool_calls = getattr(message, "tool_calls", None) or []
    if tool_calls:
        names = [str(tc.get("name")) for tc in tool_calls if isinstance(tc, dict)]
        parts.append("请求工具=" + ",".join(names))
        # 结构化输出失败时,真正要看的是模型给的参数长什么样
        first_args = tool_calls[0].get("args") if isinstance(tool_calls[0], dict) else None
        if first_args is not None:
            parts.append("首参=" + _short(json.dumps(first_args, ensure_ascii=False, default=str), 220))

    usage = getattr(message, "usage_metadata", None) or {}
    if usage:
        parts.append(
            f"tokens(in/out)={usage.get('input_tokens', '?')}/{usage.get('output_tokens', '?')}"
        )

    parts.append(f"输出{len(_message_text(message))}字")

    metadata = getattr(message, "response_metadata", None) or {}
    finish = metadata.get("finish_reason")
    if finish:
        parts.append(f"finish={finish}")

    return " | ".join(parts)


def _summarize_messages(messages: Any, limit: int = 400) -> List[str]:
    """把 Agent 的消息序列压成可读的几行(人/模型/工具各一行)。"""
    lines: List[str] = []
    if not isinstance(messages, list):
        return lines

    for index, message in enumerate(messages):
        kind = type(message).__name__
        name = getattr(message, "name", None) or ""
        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            calls = ", ".join(
                f"{tc.get('name')}({_short(json.dumps(tc.get('args'), ensure_ascii=False, default=str), 200)})"
                for tc in tool_calls
                if isinstance(tc, dict)
            )
            lines.append(f"    #{index} {kind} 工具调用: {_short(calls, limit)}")
            continue
        content = _message_text(message)
        status = getattr(message, "status", None)
        suffix = f" [status={status}]" if status else ""
        lines.append(f"    #{index} {kind}{(' ' + name) if name else ''}{suffix}: {_short(content, limit)}")
    return lines


def _tool_output_text(output: Any) -> str:
    """把 LangChain / MCP 工具返回值归一化成文本。"""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(
            (item.get("text", "") if isinstance(item, dict) else str(item))
            for item in output
        )
    return str(output)


def _tool_note(tool_input: Any, output: Any) -> str:
    if isinstance(tool_input, (dict, list)):
        args = json.dumps(tool_input, ensure_ascii=False)
    else:
        args = str(tool_input)

    text = _tool_output_text(output)
    return f"参数={_short(args, 80)} | 返回{len(text)}字"


def _poi_location_stats(text: str) -> Dict[str, int]:
    """统计高德文本搜索结果里有几个 POI 真的带了坐标(对应 P1 已知 bug)。"""
    total = 0
    with_location = 0

    data: Any = None
    with contextlib.suppress(Exception):
        data = json.loads(text)

    def walk(node: Any) -> None:
        nonlocal total, with_location
        if isinstance(node, dict):
            if "id" in node and ("name" in node or "typecode" in node):
                total += 1
                if str(node.get("location", "")).strip():
                    with_location += 1
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    if data is not None:
        walk(data)
    else:
        # 解析不出 JSON 就用正则粗查,只为了知道"坐标是不是空的"
        for match in re.finditer(r'"location"\s*:\s*"([^"]*)"', text):
            total += 1
            if match.group(1).strip():
                with_location += 1

    return {"total": total, "with_location": with_location}


# ============ 事件流计时 ============


def _consume_event(
    event: Dict[str, Any], started: float, pending: Dict[Any, tuple], stage: Stage
) -> None:
    kind = event.get("event")
    run_id = event.get("run_id")
    now = time.monotonic() - started
    data = event.get("data") or {}

    if kind == "on_chat_model_start":
        pending[run_id] = ("llm", event.get("name") or "chat_model", now)
        return

    if kind == "on_chat_model_end":
        info = pending.pop(run_id, None)
        name = info[1] if info else (event.get("name") or "chat_model")
        begin = info[2] if info else now
        output = data.get("output")
        stage.steps.append(Step("llm", name, begin, now - begin, _llm_note(output)))
        text = _message_text(_extract_message(output))
        if text.strip():
            stage.text = text
        return

    if kind == "on_tool_start":
        pending[run_id] = ("tool", event.get("name") or "tool", now)
        return

    if kind == "on_tool_end":
        info = pending.pop(run_id, None)
        name = info[1] if info else (event.get("name") or "tool")
        begin = info[2] if info else now
        stage.steps.append(
            Step(
                "tool",
                name,
                begin,
                now - begin,
                _tool_note(data.get("input"), data.get("output")),
            )
        )
        return

    if kind == "on_chain_end":
        output = data.get("output")
        if isinstance(output, dict):
            if output.get("structured_response") is not None:
                stage.extra["structured_response"] = output["structured_response"]
            messages = output.get("messages")
            if isinstance(messages, list) and messages:
                stage.messages = messages
                text = _message_text(messages[-1])
                if text.strip():
                    stage.text = text


async def run_agent_traced(
    agent: Any,
    query: str,
    stage: Stage,
    *,
    timeout: float,
    recursion_limit: int,
) -> Stage:
    """执行一个 Agent,同时按事件流记录每次 LLM / 工具调用耗时。"""
    stage.timeout = timeout
    started = time.monotonic()
    pending: Dict[Any, tuple] = {}

    stream = agent.astream_events(
        {"messages": [{"role": "user", "content": query}]},
        config={"recursion_limit": recursion_limit},
    )

    try:
        async with asyncio.timeout(timeout):
            async for event in stream:
                _consume_event(event, started, pending, stage)
    except TimeoutError:
        stage.timed_out = True
    except Exception as exc:  # noqa: BLE001 - 诊断脚本要原样看到任何异常
        stage.error = f"{type(exc).__name__}: {exc}"
    finally:
        stage.wall = time.monotonic() - started
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()

    return stage


async def run_agent_capture(
    agent: Any,
    query: str,
    stage: Stage,
    *,
    timeout: float,
    recursion_limit: int,
) -> Stage:
    """按状态快照跑一个 Agent:能拿到**中间**消息(含校验失败的 ToolMessage)。

    ``astream_events`` 只在图正常跑完时才给出最终状态,而规划 Agent 恰恰是
    "跑到超时都没跑完"的那种 —— 超时后什么都看不到。改用 ``stream_mode="values"``
    逐帧拿状态,即便中途被超时打断,已产出的消息也留在 ``stage.messages`` 里。
    """
    stage.timeout = timeout
    started = time.monotonic()
    marks: List[Tuple[int, float]] = []

    stream = agent.astream(
        {"messages": [{"role": "user", "content": query}]},
        config={"recursion_limit": recursion_limit},
        stream_mode="values",
    )

    try:
        async with asyncio.timeout(timeout):
            async for state in stream:
                if not isinstance(state, dict):
                    continue
                messages = state.get("messages")
                if isinstance(messages, list) and messages:
                    if len(messages) != len(stage.messages):
                        marks.append((len(messages), round(time.monotonic() - started, 1)))
                    stage.messages = list(messages)
                structured = state.get("structured_response")
                if structured is not None:
                    stage.extra["structured_response"] = structured
    except TimeoutError:
        stage.timed_out = True
    except Exception as exc:  # noqa: BLE001 - 诊断脚本要原样看到任何异常
        stage.error = f"{type(exc).__name__}: {exc}"
    finally:
        stage.wall = time.monotonic() - started
        stage.extra["message_marks"] = marks
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()

    return stage


# ============ 各阶段 ============


async def stage_tool_direct(
    tools: Sequence[Any], city: str, keywords: str, *, timeout: float
) -> Stage:
    """直接调用 ``maps_text_search``,不经模型 —— 量出工具本身的纯耗时。"""
    stage = Stage(label="直接调用 maps_text_search(不经模型)", timeout=timeout)
    tool = find_tool(list(tools), "maps_text_search")
    if tool is None:
        stage.error = "未找到 maps_text_search 工具"
        return stage

    schema = getattr(tool, "args_schema", None)
    if schema is not None:
        with contextlib.suppress(Exception):
            properties = schema.model_json_schema().get("properties", {})
            stage.extra["schema"] = {
                name: meta.get("type") for name, meta in properties.items()
            }

    # 注意:高德 MCP 的 citylimit 是**字符串**类型("true"/"false"),
    # 传布尔会被工具参数校验直接拒绝(实测踩过)。
    candidates: List[Dict[str, Any]] = [
        {"keywords": keywords, "city": city, "citylimit": "true"},
        {"keywords": keywords, "city": city},
        {"keywords": f"{city}{keywords}"},
    ]

    started = time.monotonic()
    last_error = ""
    for args in candidates:
        try:
            async with asyncio.timeout(timeout):
                raw = await tool.ainvoke(args)
        except TimeoutError:
            stage.timed_out = True
            stage.wall = time.monotonic() - started
            return stage
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            continue

        text = _tool_output_text(raw)
        # 工具把参数校验失败也当成正常返回值("Error executing tool ..."),
        # 所以这里还要看内容,不能只看异常。
        if text.startswith("Error executing tool"):
            last_error = _short(text, 160)
            continue

        duration = time.monotonic() - started
        stage.wall = duration
        stage.steps.append(Step("tool", tool.name, 0.0, duration, _tool_note(args, raw)))
        stage.text = text
        stage.extra["used_args"] = args
        stage.extra["location_stats"] = _poi_location_stats(text)
        return stage

    stage.wall = time.monotonic() - started
    stage.error = f"三种参数组合都失败,最后一次: {last_error}"
    return stage


async def stage_retrieval(request: TripRequest, *, timeout: float) -> Stage:
    """三路检索(确定性,不经过模型):分别计时景点 / 天气 / 酒店。"""
    stage = Stage(label="三路检索(确定性,0 次模型调用)", timeout=timeout)
    started = time.monotonic()

    for label, func in (
        ("景点", retrieve_attractions),
        ("天气", retrieve_weather),
        ("酒店", retrieve_hotels),
    ):
        begin = time.monotonic() - started
        try:
            async with asyncio.timeout(timeout):
                text = await func(request)
            note = f"输出 {len(text)} 字" + ("" if text else "(空结果)")
        except TimeoutError:
            stage.timed_out = True
            stage.wall = time.monotonic() - started
            return stage
        except Exception as exc:  # noqa: BLE001
            text = ""
            note = f"失败 {type(exc).__name__}: {exc}"

        stage.steps.append(
            Step("retrieve", label, begin, time.monotonic() - started - begin, note)
        )
        stage.extra[f"{label}预览"] = _short(text, 160)

    stage.wall = time.monotonic() - started
    return stage


async def stage_detail_direct(
    tools: Sequence[Any],
    city: str,
    keywords: str,
    *,
    timeout: float,
    concurrency: int,
    serial: int = 2,
) -> Stage:
    """量 ``maps_search_detail`` 的纯耗时:先串行两次,再按 concurrency 并发一批。

    检索 Agent 实际就是这么用的 —— 一次 text_search 拿到一堆 poi_id,
    然后并发十个 detail 调用。这一段不花模型额度。
    """
    stage = Stage(label="直接调用 maps_search_detail(不经模型)", timeout=timeout)
    search = find_tool(list(tools), "maps_text_search")
    detail = find_tool(list(tools), "maps_search_detail")
    if search is None or detail is None:
        stage.error = "未找到 maps_text_search / maps_search_detail 工具"
        return stage

    started = time.monotonic()
    raw = await search.ainvoke({"keywords": keywords, "city": city, "citylimit": "true"})
    ids = re.findall(r'"id"\s*:\s*"([^"]+)"', _tool_output_text(raw))
    stage.extra["poi_ids"] = len(ids)
    if not ids:
        stage.error = "text_search 没有拿到 poi_id"
        stage.wall = time.monotonic() - started
        return stage

    async def call(poi_id: str) -> Tuple[float, str]:
        begin = time.monotonic() - started
        try:
            result = await detail.ainvoke({"id": poi_id})
            text = _tool_output_text(result)
        except Exception as exc:  # noqa: BLE001
            text = f"<{type(exc).__name__}: {exc}>"
        end = time.monotonic() - started
        stage.steps.append(Step("tool", detail.name, begin, end - begin, f"id={poi_id}"))
        return end - begin, text

    for poi_id in ids[: max(serial, 0)]:
        await call(poi_id)

    batch = ids[: max(concurrency, 0)]
    batch_start = time.monotonic()
    results = await asyncio.gather(*(call(poi_id) for poi_id in batch))
    batch_wall = time.monotonic() - batch_start

    stage.wall = time.monotonic() - started
    stage.extra["batch_size"] = len(batch)
    stage.extra["batch_wall"] = round(batch_wall, 1)
    if results:
        stage.extra["detail_location_stats"] = _poi_location_stats(results[-1][1])
        stage.extra["detail_sample"] = _short(results[-1][1], 300)
    return stage


async def stage_planner(request: TripRequest, *, timeout: float, recursion_limit: int) -> Stage:
    """单独计时规划 Agent(结构化输出),喂固定检索结果,不联网、不挂工具。"""
    stage = Stage(label="规划 Agent(结构化输出)", timeout=timeout)

    # 复用生产代码里的查询构造,保证和真实链路喂给模型的东西一致。
    # 该方法不依赖实例状态,所以 self 传 None 也不会有副作用。
    query = MultiAgentTripPlanner._build_planner_query(  # noqa: SLF001
        None, request, CANNED_ATTRACTIONS, CANNED_WEATHER, CANNED_HOTELS
    )
    stage.extra["query_chars"] = len(query)

    agent = create_agent(
        model=get_llm(),
        tools=[],
        system_prompt=PLANNER_AGENT_PROMPT,
        response_format=TripPlan,
        name="planner_agent",
    )
    # 这里刻意用 capture 而不是 traced:规划 Agent 经常是"跑到超时都没收敛",
    # 必须能看到中间消息(尤其是校验失败时回灌的 ToolMessage)。
    await run_agent_capture(
        agent, query, stage, timeout=timeout, recursion_limit=recursion_limit
    )

    # 结构化输出成不成功,是这一阶段第二想知道的
    structured = stage.extra.pop("structured_response", None)
    if structured is None:
        stage.extra["structured"] = False
    elif isinstance(structured, TripPlan):
        stage.extra["structured"] = True
        stage.extra["structured_days"] = len(structured.days)
        stage.extra["structured_attractions"] = sum(
            len(day.attractions) for day in structured.days
        )
    else:
        try:
            plan = TripPlan.model_validate(structured)
            stage.extra["structured"] = True
            stage.extra["structured_days"] = len(plan.days)
            stage.extra["structured_attractions"] = sum(
                len(day.attractions) for day in plan.days
            )
        except Exception as exc:  # noqa: BLE001
            stage.extra["structured"] = False
            stage.extra["structured_error"] = f"{type(exc).__name__}: {exc}"

    return stage


async def stage_graph(request: TripRequest, *, timeout: float, recursion_limit: int) -> Stage:
    """真实全链路(三个检索 Agent 并行 + 规划 Agent),费用最高。"""
    stage = Stage(label="真实全链路 plan_trip_with_status", timeout=timeout)
    planner_sys = MultiAgentTripPlanner()
    started = time.monotonic()
    try:
        async with asyncio.timeout(timeout):
            outcome = await planner_sys.plan_trip_with_status(request)
        stage.extra["degraded"] = outcome.degraded
        stage.extra["reason"] = outcome.reason
        stage.extra["days"] = len(outcome.plan.days)
    except TimeoutError:
        stage.timed_out = True
    except Exception as exc:  # noqa: BLE001
        stage.error = f"{type(exc).__name__}: {exc}"
    finally:
        stage.wall = time.monotonic() - started

    return stage


# ============ 输出 ============


def _has_overlap(steps: Sequence[Step]) -> bool:
    """判断步骤之间是否有时间重叠(有重叠就说明是并发执行)。"""
    ordered = sorted(steps, key=lambda step: step.start)
    for previous, current in zip(ordered, ordered[1:]):
        if current.start < previous.start + previous.duration - 0.05:
            return True
    return False


def print_stage(stage: Stage, *, dump_messages: bool = False) -> None:
    head = f"\n【{stage.label}】墙钟耗时 {stage.wall:.1f}s"
    if stage.timed_out:
        head += f"  [超时中断 > {stage.timeout:.0f}s]"
    if stage.error:
        head += f"  [失败] {stage.error}"
    print(head)

    if stage.llm_steps:
        print(f"  LLM 调用 {len(stage.llm_steps)} 次,合计 {stage.llm_total:.1f}s")
        for idx, step in enumerate(stage.llm_steps, 1):
            print(f"    {idx}) {step.duration:7.1f}s  [{step.name}] {step.note}")
    else:
        print("  LLM 调用 0 次")

    if stage.tool_steps:
        print(f"  MCP 工具调用 {len(stage.tool_steps)} 次,合计 {stage.tool_total:.1f}s")
        for idx, step in enumerate(stage.tool_steps, 1):
            print(f"    {idx}) {step.duration:7.1f}s  {step.name}  {step.note}")

    if stage.retrieval_steps:
        print(
            f"  确定性检索 {len(stage.retrieval_steps)} 段,"
            f"合计 {stage.retrieval_total:.1f}s(0 次模型调用)"
        )
        for idx, step in enumerate(stage.retrieval_steps, 1):
            print(f"    {idx}) {step.duration:7.1f}s  {step.name}  {step.note}")

    if stage.wall and stage.steps and _has_overlap(stage.steps):
        print("  注:存在并发执行的调用,逐项耗时之和大于墙钟耗时是正常的")
    elif stage.wall and (stage.llm_steps or stage.tool_steps):
        other = stage.wall - stage.llm_total - stage.tool_total - stage.retrieval_total
        print(f"  其它开销(编排/事件流/解析): {other:.1f}s")

    for key, value in stage.extra.items():
        print(f"  · {key}: {value}")

    if stage.text:
        print(f"  最终文本({len(stage.text)}字)预览: {_short(stage.text, 200)}")

    if dump_messages and stage.messages:
        print(f"  消息序列({len(stage.messages)} 条):")
        for line in _summarize_messages(stage.messages):
            print(line)


def print_summary(stages: Sequence[Stage]) -> None:
    print("\n" + "=" * 72)
    print("汇总")
    print("=" * 72)
    for stage in stages:
        flag = " [已按 --timeout 中断,真实耗时 >= 该值]" if stage.timed_out else ""
        print(
            f"  {stage.label}: {stage.wall:.1f}s"
            f"(LLM {len(stage.llm_steps)} 次 {stage.llm_total:.1f}s,"
            f" 工具 {len(stage.tool_steps)} 次 {stage.tool_total:.1f}s){flag}"
        )


# ============ 入口 ============


def build_request(args: argparse.Namespace) -> TripRequest:
    return TripRequest(
        city=args.city,
        start_date="2025-06-01",
        end_date="2025-06-02",
        travel_days=2,
        transportation=args.transportation,
        accommodation="经济型酒店",
        preferences=args.preferences,
        free_text_input="",
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P0 耗时隔离诊断")
    parser.add_argument(
        "--stages",
        default="tool,retrieval,planner",
        help=(
            "要跑的阶段,逗号分隔。可选 tool/detail/retrieval/planner/graph。"
            "tool/detail/retrieval 都不调用大模型(零额度);"
            "graph 是真实全链路,现在也只要 1~2 次模型调用"
        ),
    )
    parser.add_argument("--city", default=DEFAULT_CITY)
    parser.add_argument("--keywords", default=DEFAULT_KEYWORDS)
    parser.add_argument("--preferences", nargs="*", default=DEFAULT_PREFERENCES)
    parser.add_argument("--transportation", default="公共交通")
    parser.add_argument("--timeout", type=float, default=300.0, help="单个阶段的墙钟上限(秒)")
    parser.add_argument(
        "--recursion-limit",
        type=int,
        default=25,
        help="Agent 循环上限(生产代码目前没传,默认 25,便于观察循环次数)",
    )
    parser.add_argument(
        "--detail-concurrency",
        type=int,
        default=10,
        help="detail 阶段并发调用的数量(复现检索 Agent 的实际用法)",
    )
    parser.add_argument(
        "--dump-messages",
        action="store_true",
        help="打印 Agent 的完整消息序列(定位结构化输出为何不收敛时非常有用)",
    )
    parser.add_argument(
        "--mcp-log",
        default=str(Path(tempfile.gettempdir()) / "trip_planner_mcp_diag.log"),
        help="常驻 MCP Server 子进程的 stdout/stderr 落盘位置(默认落临时目录)",
    )
    return parser.parse_args(argv)


def quiet_mcp_subprocess(log_path: str) -> None:
    """把常驻 MCP Server 子进程的输出重定向到文件。

    默认情况下子进程会继承本进程的 stdout,它的 uvicorn access log 会把诊断
    结果整个淹掉(实测几千行)。这里只替换 ``amap_mcp`` 模块看到的 ``Popen``,
    不影响本进程其它地方的 subprocess 调用。
    """
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("wb")
    real_popen = subprocess.Popen

    class _QuietPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("stdout", handle)
            kwargs.setdefault("stderr", handle)
            super().__init__(*args, **kwargs)

    class _SubprocessProxy:
        Popen = _QuietPopen

        def __getattr__(self, name: str) -> Any:
            return getattr(subprocess, name)

    amap_mcp.subprocess = _SubprocessProxy()  # type: ignore[assignment]
    print(f"MCP Server 子进程日志 -> {path}")


async def main_async(args: argparse.Namespace) -> int:
    stages_requested = [s.strip() for s in args.stages.split(",") if s.strip()]

    llm = get_llm()
    print(f"工作目录: {Path.cwd()}")
    print(f"模型: {getattr(llm, 'model_name', None) or getattr(llm, 'model', '?')}")
    print(f"单阶段超时: {args.timeout:.0f}s | recursion_limit: {args.recursion_limit}")
    print(f"阶段: {', '.join(stages_requested)}")

    results: List[Stage] = []

    tools: List[Any] = []
    if any(key != "planner" for key in stages_requested):
        print("\n--- 准备常驻高德 MCP Server 与工具 ---")
        quiet_mcp_subprocess(args.mcp_log)
        started = time.monotonic()
        url = await ensure_amap_server()
        tools = await get_amap_tools()
        print(f"MCP Server: {url}")
        print(f"工具加载 {len(tools)} 个,耗时 {time.monotonic() - started:.1f}s")

    try:
        for key in stages_requested:
            if key == "tool":
                stage = await stage_tool_direct(
                    tools, args.city, args.keywords, timeout=args.timeout
                )
            elif key == "detail":
                stage = await stage_detail_direct(
                    tools,
                    args.city,
                    args.keywords,
                    timeout=args.timeout,
                    concurrency=args.detail_concurrency,
                )
            elif key == "retrieval":
                stage = await stage_retrieval(build_request(args), timeout=args.timeout)
            elif key == "planner":
                stage = await stage_planner(
                    build_request(args),
                    timeout=args.timeout,
                    recursion_limit=args.recursion_limit,
                )
            elif key == "graph":
                stage = await stage_graph(
                    build_request(args),
                    timeout=args.timeout,
                    recursion_limit=args.recursion_limit,
                )
            else:
                print(f"未知阶段,已跳过: {key}")
                continue

            results.append(stage)
            print_stage(stage, dump_messages=args.dump_messages)
    finally:
        shutdown_amap_server()

    print_summary(results)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n已中断")
        shutdown_amap_server()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
