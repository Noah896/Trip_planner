"""JSON 解析工具

用于从 LLM / MCP 工具返回的文本中稳健地抽取 JSON 数据。
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional


def extract_json(text: str) -> Optional[Any]:
    """从任意文本中抽取第一个可解析的 JSON 对象或数组。

    依次尝试:
    1. Markdown 代码块(```json ... ```)中的内容
    2. 全文
    3. 全文里第一个 ``{...}`` / ``[...]`` 片段

    Args:
        text: 原始文本

    Returns:
        解析后的 Python 对象;全部失败时返回 None
    """
    if not text:
        return None

    candidates: List[str] = []

    # 1. Markdown 代码块
    for block in re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL):
        candidates.append(block.strip())

    # 2. 全文
    stripped = text.strip()
    candidates.append(stripped)

    # 3. 括号切片
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = stripped.find(opener), stripped.rfind(closer)
        if 0 <= start < end:
            candidates.append(stripped[start : end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

    return None


def parse_mcp_payload(text: str) -> Optional[Any]:
    """解析 MCP 工具返回文本。

    MCP 的 ``CallToolResult`` 可能被包成
    ``{"content": [{"type": "text", "text": "<json>"}]}``,
    这里会自动下钻一层,取出真正的业务数据。
    """
    data = extract_json(text)
    if isinstance(data, dict) and "content" in data:
        texts = [
            block.get("text", "")
            for block in data.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        nested = extract_json("\n".join(t for t in texts if t))
        if nested is not None:
            return nested
    return data


def json_dumps(data: Any) -> str:
    """以 UTF-8 友好方式序列化 JSON(用于回填到提示词)。"""
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)

