#!/usr/bin/env python
"""运行公共旅行知识 RAG 的离线检索评测。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.rag import KnowledgeConfig, KnowledgeService  # noqa: E402
from evals.rag_evaluator import (  # noqa: E402
    DEFAULT_RAG_CASES_PATH,
    build_rag_report,
    evaluate_search_case,
    load_rag_cases,
    load_seed_doc_cities,
    render_rag_markdown,
    threshold_failures,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="评测旅行知识 RAG 检索质量")
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_RAG_CASES_PATH,
        help="评测问题 JSONL",
    )
    parser.add_argument(
        "--seed",
        type=Path,
        default=ROOT / "knowledge" / "seed_documents.jsonl",
        help="知识种子 JSONL",
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=None,
        help="FAISS 索引目录;默认读取 RAG_INDEX_PATH",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "evals" / "results" / "rag",
        help="JSON / Markdown 报告目录",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="忽略已有索引,重新执行 BGE 向量化",
    )
    parser.add_argument(
        "--min-hit-at-5",
        type=float,
        default=None,
        help="可选 Hit@5 下限;不传只记录基线",
    )
    parser.add_argument(
        "--max-no-answer-false-positive-rate",
        type=float,
        default=None,
        help="可选无答案误命中率上限;不传只记录基线",
    )
    parser.add_argument(
        "--max-p95-ms",
        type=float,
        default=None,
        help="可选检索 P95 延迟上限(毫秒)",
    )
    return parser.parse_args()


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path


def main() -> int:
    args = _parse_args()
    settings = get_settings()
    seed_path = _resolve(args.seed)
    cases_path = _resolve(args.cases)
    index_path = _resolve(args.index or Path(settings.rag_index_path))

    known_docs = load_seed_doc_cities(seed_path)
    cases = load_rag_cases(
        cases_path,
        known_doc_cities=known_docs,
    )

    service = KnowledgeService(
        KnowledgeConfig(
            index_path=index_path,
            seed_path=seed_path,
            embedding_model=settings.rag_embedding_model,
            model_root=settings.rag_model_root or None,
            device=settings.rag_device,
            top_k=max(5, settings.rag_top_k),
            vector_k=max(5, settings.rag_vector_k),
            bm25_k=max(5, settings.rag_bm25_k),
            rrf_k=settings.rag_rrf_k,
            timeout_seconds=max(30.0, settings.rag_timeout_seconds),
            auto_bootstrap=settings.rag_auto_bootstrap,
            min_vector_relevance=settings.rag_min_vector_relevance,
            lexical_rescue_min_vector_relevance=(
                settings.rag_lexical_rescue_min_vector_relevance
            ),
            lexical_rescue_min_bm25_score=(
                settings.rag_lexical_rescue_min_bm25_score
            ),
        )
    )
    service.initialize(force_rebuild=args.rebuild)
    stats = service.stats()

    results = []
    for case in cases:
        search = service.search_sync(
            case["query"],
            city=case["city"],
            as_of=case["as_of"],
        )
        results.append(evaluate_search_case(case, search))

    report = build_rag_report(
        results,
        config={
            "embedding_model": settings.rag_embedding_model,
            "top_k": max(5, settings.rag_top_k),
            "vector_k": max(5, settings.rag_vector_k),
            "bm25_k": max(5, settings.rag_bm25_k),
            "rrf_k": settings.rag_rrf_k,
            "min_vector_relevance": settings.rag_min_vector_relevance,
            "lexical_rescue_min_vector_relevance": (
                settings.rag_lexical_rescue_min_vector_relevance
            ),
            "lexical_rescue_min_bm25_score": (
                settings.rag_lexical_rescue_min_bm25_score
            ),
            "cases_path": str(cases_path),
            "seed_path": str(seed_path),
        },
        index_stats=stats,
    )

    output_dir = _resolve(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "latest.json"
    markdown_path = output_dir / "latest.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(
        render_rag_markdown(report),
        encoding="utf-8",
    )

    print(
        "RAG 评测完成: "
        f"cases={report['case_count']}, "
        f"Hit@5={report['hit_at_5']}, "
        f"MRR@5={report['mrr_at_5']}, "
        "no-answer-FP="
        f"{report['no_answer_false_positive_rate']}, "
        f"P95={report['latency_ms']['p95']}ms"
    )
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")

    failures = threshold_failures(
        report,
        min_hit_at_5=args.min_hit_at_5,
        max_no_answer_false_positive_rate=(
            args.max_no_answer_false_positive_rate
        ),
        max_p95_ms=args.max_p95_ms,
    )
    for failure in failures:
        print(f"❌ 阈值失败: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
