#!/usr/bin/env python
"""离线检查 C8 风格旅行知识 RAG 召回结果。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.rag import KnowledgeConfig, KnowledgeService  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检查旅行知识 RAG")
    parser.add_argument("--query", required=True)
    parser.add_argument("--city", required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    settings = get_settings()
    index_path = Path(settings.rag_index_path)
    if not index_path.is_absolute():
        index_path = ROOT / index_path
    seed_path = Path(settings.rag_seed_path)
    if not seed_path.is_absolute():
        seed_path = ROOT / seed_path

    service = KnowledgeService(
        KnowledgeConfig(
            index_path=index_path,
            seed_path=seed_path,
            embedding_model=settings.rag_embedding_model,
            model_root=settings.rag_model_root or None,
            device=settings.rag_device,
            top_k=settings.rag_top_k,
            vector_k=settings.rag_vector_k,
            bm25_k=settings.rag_bm25_k,
            rrf_k=settings.rag_rrf_k,
            timeout_seconds=settings.rag_timeout_seconds,
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
    service.initialize()
    result = service.search_sync(
        args.query,
        city=args.city,
        as_of=args.as_of,
    )
    print(
        json.dumps(
            {
                "query": args.query,
                "city": args.city,
                "as_of": args.as_of.isoformat(),
                "vector_candidates": result.vector_candidates,
                "bm25_candidates": result.bm25_candidates,
                "elapsed_ms": result.elapsed_ms,
                "chunks": [
                    {
                        "chunk_id": chunk.chunk_id,
                        "title": chunk.title,
                        "city": chunk.city,
                        "section": chunk.section,
                        "score": chunk.score,
                    }
                    for chunk in result.chunks
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
