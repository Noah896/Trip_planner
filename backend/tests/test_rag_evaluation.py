"""公共旅行知识 RAG 检索评测器测试。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from evals.rag_evaluator import (
    DEFAULT_RAG_CASES_PATH,
    build_rag_report,
    evaluate_search_case,
    load_rag_cases,
    load_seed_doc_cities,
    render_rag_markdown,
    threshold_failures,
)
from app.rag import KnowledgeSearchResult


@dataclass
class _Chunk:
    doc_id: str
    title: str
    city: str
    score: float = 0.1
    vector_relevance: float = 0.1
    bm25_score: float = 0.1


def _result(*chunks: _Chunk, elapsed_ms: float = 10.0):
    return KnowledgeSearchResult(
        chunks=list(chunks),
        vector_candidates=5,
        bm25_candidates=5,
        elapsed_ms=elapsed_ms,
    )


def _case(
    case_id: str,
    expected: list[str],
    *,
    city: str = "北京",
) -> dict:
    return {
        "id": case_id,
        "query": case_id,
        "city": city,
        "category": "test",
        "as_of": None,
        "expected_doc_ids": expected,
        "answerable": bool(expected),
        "notes": "",
    }


def test_repository_eval_cases_cover_seed_cities_and_no_answer_paths():
    seed_path = (
        Path(__file__).resolve().parent.parent
        / "knowledge"
        / "seed_documents.jsonl"
    )
    docs = load_seed_doc_cities(seed_path)
    cases = load_rag_cases(
        DEFAULT_RAG_CASES_PATH,
        known_doc_cities=docs,
    )

    assert 30 <= len(cases) <= 50
    assert {case["city"] for case in cases} == set(docs.values())
    assert any(case["answerable"] for case in cases)
    assert any(not case["answerable"] for case in cases)
    assert {
        "exact_fact",
        "synonym",
        "time_sensitive",
        "realtime_boundary",
        "no_answer",
        "city_conflict",
    } <= {case["category"] for case in cases}


def test_load_rag_cases_rejects_unknown_or_wrong_city_documents(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text(
        '{"id":"bad","query":"故宫","city":"上海",'
        '"category":"test","expected_doc_ids":["beijing-palace"]}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="不属于 上海"):
        load_rag_cases(
            path,
            known_doc_cities={"beijing-palace": "北京"},
        )


def test_evaluate_search_case_computes_rank_false_positive_and_city_leak():
    hit_case = _case("hit", ["expected"])
    hit = evaluate_search_case(
        hit_case,
        _result(_Chunk("other", "其它", "北京"), _Chunk("expected", "正确", "北京")),
    )
    miss_case = _case("miss", ["expected"])
    miss = evaluate_search_case(
        miss_case,
        _result(_Chunk("other", "其它", "北京")),
    )
    no_answer_case = _case("no-answer", [])
    false_positive = evaluate_search_case(
        no_answer_case,
        _result(_Chunk("unrelated", "无关", "北京")),
    )
    city_leak_case = _case("city-leak", ["expected"])
    city_leak = evaluate_search_case(
        city_leak_case,
        _result(_Chunk("expected", "正确", "上海")),
    )

    assert hit["rank"] == 2
    assert hit["reciprocal_rank"] == 0.5
    assert hit["passed"] is True
    assert miss["hit"] is False
    assert miss["passed"] is False
    assert false_positive["false_positive"] is True
    assert false_positive["passed"] is False
    assert city_leak["city_violations"] == ["expected"]
    assert city_leak["passed"] is False


def test_build_report_and_thresholds():
    results = [
        evaluate_search_case(
            _case("hit", ["expected"]),
            _result(
                _Chunk("other", "其它", "北京"),
                _Chunk("expected", "正确", "北京"),
                elapsed_ms=20.0,
            ),
        ),
        evaluate_search_case(
            _case("miss", ["expected"]),
            _result(_Chunk("other", "其它", "北京"), elapsed_ms=10.0),
        ),
        evaluate_search_case(
            _case("no-answer", []),
            _result(_Chunk("unrelated", "无关", "北京"), elapsed_ms=30.0),
        ),
    ]
    report = build_rag_report(
        results,
        config={"model": "fake"},
        index_stats={"documents": 3},
    )

    assert report["hit_at_5"] == 0.5
    assert report["mrr_at_5"] == 0.25
    assert report["no_answer_false_positive_rate"] == 1.0
    assert report["latency_ms"] == {"p50": 20.0, "p95": 30.0, "max": 30.0}
    assert "Hit@5" in render_rag_markdown(report)
    assert threshold_failures(
        report,
        min_hit_at_5=0.8,
        max_no_answer_false_positive_rate=0.0,
        max_p95_ms=20.0,
    )
