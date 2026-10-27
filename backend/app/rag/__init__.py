"""应用内独立 RAG 子包，采用 C8 的 BGE + FAISS + BM25 + RRF。"""

from .data_preparation import (
    TravelKnowledgePreparation,
    content_hash,
    normalize_text,
)
from .embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    create_huggingface_embeddings,
    resolve_local_model,
)
from .index import KnowledgeIndex
from .retrieval import HybridRetriever, HybridSearchResult
from .service import (
    KnowledgeConfig,
    KnowledgeService,
    format_knowledge_evidence,
)
from .types import (
    KnowledgeChunk,
    KnowledgeResult,
    KnowledgeSearchResult,
    KnowledgeSourceRef,
)

__all__ = [
    "DEFAULT_EMBEDDING_MODEL",
    "HybridRetriever",
    "HybridSearchResult",
    "KnowledgeChunk",
    "KnowledgeConfig",
    "KnowledgeIndex",
    "KnowledgeResult",
    "KnowledgeSearchResult",
    "KnowledgeService",
    "KnowledgeSourceRef",
    "TravelKnowledgePreparation",
    "content_hash",
    "create_huggingface_embeddings",
    "format_knowledge_evidence",
    "normalize_text",
    "resolve_local_model",
]
