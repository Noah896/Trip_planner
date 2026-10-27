#!/usr/bin/env python
"""按 C8 的 BGE + FAISS 方案构建旅行知识索引。"""

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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建 C8 风格旅行知识索引")
    parser.add_argument(
        "--input",
        type=Path,
        default=ROOT / "knowledge" / "seed_documents.jsonl",
        help="知识文档 JSONL",
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=None,
        help="FAISS 索引目录;默认读取 RAG_INDEX_PATH",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="忽略已有 FAISS，重新执行 BGE 向量化",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="构建后打印索引统计",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    settings = get_settings()
    index_path = args.index or Path(settings.rag_index_path)
    if not index_path.is_absolute():
        index_path = ROOT / index_path

    service = KnowledgeService(
        KnowledgeConfig(
            index_path=index_path,
            seed_path=args.input,
            embedding_model=settings.rag_embedding_model,
            model_root=settings.rag_model_root or None,
            device=settings.rag_device,
            top_k=settings.rag_top_k,
            vector_k=settings.rag_vector_k,
            bm25_k=settings.rag_bm25_k,
            rrf_k=settings.rag_rrf_k,
            auto_bootstrap=True,
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
    print(
        f"索引完成: {stats['documents']} 个文档、"
        f"{stats['chunks']} 个切片"
    )
    print(
        "召回配置: "
        f"{stats['retrieval']['method']}, "
        f"vector_k={stats['retrieval']['vector_k']}, "
        f"bm25_k={stats['retrieval']['bm25_k']}, "
        f"rrf_k={stats['retrieval']['rrf_k']}"
    )
    if args.stats:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
