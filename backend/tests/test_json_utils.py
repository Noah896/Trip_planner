"""``app.utils.json_utils`` 单元测试

这两个函数是"LLM 输出 → 业务数据"的唯一入口,历史上出过
markdown 代码块 / MCP 包装层解析失败的问题,因此单独覆盖。
"""

from app.utils.json_utils import extract_json, json_dumps, parse_mcp_payload


class TestExtractJson:
    def test_parses_plain_object(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_parses_plain_array(self):
        assert extract_json("[1, 2, 3]") == [1, 2, 3]

    def test_parses_markdown_code_block(self):
        text = '这里是结果:\n```json\n{"city": "北京", "days": []}\n```\n以上。'
        assert extract_json(text) == {"city": "北京", "days": []}

    def test_parses_code_block_without_language_tag(self):
        assert extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_extracts_embedded_object_from_prose(self):
        text = '好的,计划如下: {"city": "上海", "n": 2} 请查收。'
        assert extract_json(text) == {"city": "上海", "n": 2}

    def test_prefers_first_parsable_candidate(self):
        text = '{"first": 1}\n{"second": 2}'
        assert extract_json(text) == {"first": 1}

    def test_returns_none_on_unparsable_text(self):
        assert extract_json("模型没有返回 JSON,只是一段话。") is None

    def test_returns_none_on_empty_input(self):
        assert extract_json("") is None
        assert extract_json(None) is None

    def test_returns_none_on_broken_json(self):
        assert extract_json('{"a": 1,}') is None


class TestParseMcpPayload:
    def test_unwraps_content_text_block(self):
        payload = '{"content": [{"type": "text", "text": "{\\"pois\\": [1]}"}]}'
        assert parse_mcp_payload(payload) == {"pois": [1]}

    def test_keeps_plain_payload_untouched(self):
        assert parse_mcp_payload('{"pois": []}') == {"pois": []}

    def test_returns_dict_when_nested_text_is_not_json(self):
        payload = '{"content": [{"type": "text", "text": "没有 JSON"}]}'
        parsed = parse_mcp_payload(payload)
        assert isinstance(parsed, dict)
        assert parsed["content"][0]["text"] == "没有 JSON"

    def test_returns_none_for_garbage(self):
        assert parse_mcp_payload("not json at all") is None


def test_json_dumps_keeps_chinese_readable():
    dumped = json_dumps({"city": "北京"})
    assert "北京" in dumped
    assert "\\u" not in dumped
