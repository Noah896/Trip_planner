"""公共旅行知识 RAG 的离线检索质量评测。

评测只调用检索和 metadata 过滤,不进入规划链路,也不调用生成模型。
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional


DEFAULT_RAG_CASES_PATH = (
    Path(__file__).resolve().parent.parent
    / "knowledge"
    / "eval_questions.jsonl"
)


def load_rag_cases(
    path: str | Path = DEFAULT_RAG_CASES_PATH,
    *,
    known_doc_cities: Optional[Mapping[str, str]] = None,
) -> List[Dict[str, Any]]:
    """读取并校验 JSONL 检索评测问题。"""
    case_path = Path(path)
    if not case_path.exists():
        raise FileNotFoundError(f"RAG 评测问题不存在: {case_path}")

    cases: List[Dict[str, Any]] = []
    seen = set()
    with case_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{case_path}:{line_number} 不是合法 JSON"
                ) from exc
            if not isinstance(raw, dict):
                raise ValueError(
                    f"{case_path}:{line_number} 必须是 JSON 对象"
                )

            case_id = str(raw.get("id") or "").strip()
            query = str(raw.get("query") or "").strip()
            city = str(raw.get("city") or "").strip()
            category = str(raw.get("category") or "").strip()
            if not case_id or not query or not city or not category:
                raise ValueError(
                    f"{case_path}:{line_number} 必须包含 "
                    "id/query/city/category"
                )
            if case_id in seen:
                raise ValueError(f"RAG 评测 id 重复: {case_id}")
            seen.add(case_id)

            try:
                as_of = date.fromisoformat(
                    str(raw.get("as_of") or "2026-11-21")
                )
            except ValueError as exc:
                raise ValueError(
                    f"{case_path}:{line_number} 的 as_of 不是 ISO 日期"
                ) from exc

            expected = raw.get("expected_doc_ids") or []
            if not isinstance(expected, list):
                raise ValueError(
                    f"{case_path}:{line_number} 的 expected_doc_ids 必须是数组"
                )
            expected_doc_ids = [
                str(value).strip()
                for value in expected
                if str(value).strip()
            ]
            if len(expected_doc_ids) != len(set(expected_doc_ids)):
                raise ValueError(
                    f"{case_path}:{line_number} 的 expected_doc_ids 有重复"
                )

            if known_doc_cities is not None:
                unknown = [
                    doc_id
                    for doc_id in expected_doc_ids
                    if doc_id not in known_doc_cities
                ]
                if unknown:
                    raise ValueError(
                        f"{case_path}:{line_number} 引用了未知文档: {unknown}"
                    )
                wrong_city = [
                    doc_id
                    for doc_id in expected_doc_ids
                    if known_doc_cities[doc_id] != city
                ]
                if wrong_city:
                    raise ValueError(
                        f"{case_path}:{line_number} 的预期文档不属于 {city}: "
                        f"{wrong_city}"
                    )

            cases.append(
                {
                    "id": case_id,
                    "query": query,
                    "city": city,
                    "category": category,
                    "as_of": as_of,
                    "expected_doc_ids": expected_doc_ids,
                    "answerable": bool(expected_doc_ids),
                    "notes": str(raw.get("notes") or "").strip(),
                }
            )

    if not cases:
        raise ValueError("RAG 评测问题不能为空")
    return cases


def load_seed_doc_cities(path: str | Path) -> Dict[str, str]:
    """读取知识种子的 doc_id -> city 映射,用于校验评测标签。"""
    seed_path = Path(path)
    if not seed_path.exists():
        raise FileNotFoundError(f"知识文件不存在: {seed_path}")

    result: Dict[str, str] = {}
    with seed_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{seed_path}:{line_number} 不是合法 JSON"
                ) from exc
            doc_id = str(raw.get("doc_id") or "").strip()
            city = str(raw.get("city") or "").strip()
            if not doc_id or not city:
                raise ValueError(
                    f"{seed_path}:{line_number} 缺少 doc_id 或 city"
                )
            if doc_id in result:
                raise ValueError(f"{seed_path}:{line_number} doc_id 重复: {doc_id}")
            result[doc_id] = city
    return result


def evaluate_search_case(
    case: Dict[str, Any],
    search_result: Any,
) -> Dict[str, Any]:
    """评估单个问题的实际召回结果。"""
    chunks = list(search_result.chunks)
    returned_doc_ids = [chunk.doc_id for chunk in chunks]
    expected = list(case["expected_doc_ids"])

    rank: Optional[int] = None
    for index, doc_id in enumerate(returned_doc_ids, 1):
        if doc_id in expected:
            rank = index
            break

    answerable = bool(case["answerable"])
    hit = rank is not None if answerable else False
    reciprocal_rank = (1.0 / rank) if rank is not None else 0.0
    city_violations = [
        chunk.doc_id
        for chunk in chunks
        if chunk.city != case["city"]
    ]
    false_positive = not answerable and bool(returned_doc_ids)
    passed = (
        hit and not city_violations
        if answerable
        else not false_positive and not city_violations
    )

    return {
        "case_id": case["id"],
        "query": case["query"],
        "city": case["city"],
        "category": case["category"],
        "answerable": answerable,
        "expected_doc_ids": expected,
        "rank": rank,
        "hit": hit,
        "reciprocal_rank": reciprocal_rank,
        "false_positive": false_positive,
        "city_violations": city_violations,
        "passed": passed,
        "elapsed_ms": float(search_result.elapsed_ms),
        "returned_doc_ids": returned_doc_ids,
        "returned_chunks": [
            {
                "doc_id": chunk.doc_id,
                "title": chunk.title,
                "city": chunk.city,
                "score": chunk.score,
                "vector_relevance": chunk.vector_relevance,
                "bm25_score": chunk.bm25_score,
            }
            for chunk in chunks
        ],
    }


def _percentile(values: List[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 1)


def build_rag_report(
    results: Iterable[Dict[str, Any]],
    *,
    config: Dict[str, Any],
    index_stats: Dict[str, Any],
) -> Dict[str, Any]:
    """汇总 Hit@5、MRR、无答案误命中和延迟。"""
    items = list(results)
    answerable = [item for item in items if item["answerable"]]
    unanswerable = [item for item in items if not item["answerable"]]
    latencies = [item["elapsed_ms"] for item in items]
    hit_count = sum(1 for item in answerable if item["hit"])
    false_positive_count = sum(
        1 for item in unanswerable if item["false_positive"]
    )
    city_violation_cases = sum(
        1 for item in items if item["city_violations"]
    )
    passed = sum(1 for item in items if item["passed"])

    return {
        "schema_version": 1,
        "case_count": len(items),
        "passed": passed,
        "pass_rate": round(100 * passed / len(items), 1) if items else 0.0,
        "answerable_count": len(answerable),
        "unanswerable_count": len(unanswerable),
        "hit_at_5_count": hit_count,
        "hit_at_5": (
            round(hit_count / len(answerable), 4) if answerable else None
        ),
        "mrr_at_5": (
            round(
                sum(item["reciprocal_rank"] for item in answerable)
                / len(answerable),
                4,
            )
            if answerable
            else None
        ),
        "no_answer_false_positive_count": false_positive_count,
        "no_answer_false_positive_rate": (
            round(false_positive_count / len(unanswerable), 4)
            if unanswerable
            else None
        ),
        "city_violation_cases": city_violation_cases,
        "city_violation_rate": (
            round(city_violation_cases / len(items), 4) if items else None
        ),
        "latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 1) if latencies else None,
        },
        "config": config,
        "index": index_stats,
        "results": items,
    }


def render_rag_markdown(report: Dict[str, Any]) -> str:
    """渲染便于人工查看的 RAG 评测报告。"""
    latency = report["latency_ms"]
    lines = [
        "# 公共旅行知识 RAG 检索评测",
        "",
        f"- 问题数：{report['case_count']}",
        f"- 有答案：{report['answerable_count']}",
        f"- 无答案：{report['unanswerable_count']}",
        f"- `Hit@5`：{_format_rate(report['hit_at_5'])}",
        f"- `MRR@5`：{_format_metric(report['mrr_at_5'])}",
        (
            "- 无答案误命中："
            f"{report['no_answer_false_positive_count']} / "
            f"{report['unanswerable_count']} "
            f"({_format_rate(report['no_answer_false_positive_rate'])})"
        ),
        (
            "- 城市串库："
            f"{report['city_violation_cases']} 个 case"
        ),
        (
            f"- 检索延迟：P50 {latency['p50']} ms / "
            f"P95 {latency['p95']} ms / Max {latency['max']} ms"
        ),
        "",
        (
            "| 状态 | Case | 分类 | 期望文档 | 实际排名 | "
            "误命中 | 延迟 | 命中文档 |"
        ),
        "| --- | --- | --- | --- | ---: | --- | ---: | --- |",
    ]
    for item in report["results"]:
        expected = ", ".join(item["expected_doc_ids"]) or "应无答案"
        returned = ", ".join(item["returned_doc_ids"]) or "-"
        lines.append(
            f"| {'PASS' if item['passed'] else 'FAIL'} | "
            f"`{item['case_id']}` | {item['category']} | {expected} | "
            f"{item['rank'] or '-'} | "
            f"{'是' if item['false_positive'] else '否'} | "
            f"{item['elapsed_ms']:.1f} ms | {returned} |"
        )
    return "\n".join(lines) + "\n"


def threshold_failures(
    report: Dict[str, Any],
    *,
    min_hit_at_5: Optional[float] = None,
    max_no_answer_false_positive_rate: Optional[float] = None,
    max_p95_ms: Optional[float] = None,
) -> List[str]:
    """按显式阈值返回失败项;未传阈值时不进行门禁。"""
    failures: List[str] = []
    if min_hit_at_5 is not None:
        actual = report["hit_at_5"]
        if actual is None or actual < min_hit_at_5:
            failures.append(f"Hit@5={actual!r}, 要求 >= {min_hit_at_5}")
    if max_no_answer_false_positive_rate is not None:
        actual = report["no_answer_false_positive_rate"]
        if (
            actual is None
            or actual > max_no_answer_false_positive_rate
        ):
            failures.append(
                "无答案误命中率="
                f"{actual!r}, 要求 <= {max_no_answer_false_positive_rate}"
            )
    if max_p95_ms is not None:
        actual = report["latency_ms"]["p95"]
        if actual is None or actual > max_p95_ms:
            failures.append(f"P95={actual!r}ms, 要求 <= {max_p95_ms}ms")
    return failures


def _format_rate(value: Optional[float]) -> str:
    return "-" if value is None else f"{100 * value:.1f}%"


def _format_metric(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.4f}"
