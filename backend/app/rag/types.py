"""RAG 包内部的稳定数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class KnowledgeChunk:
    """被召回并准备进入规划提示词的知识切片。"""

    chunk_id: str
    doc_id: str
    title: str
    section: str
    city: str
    category: str
    content: str
    source_url: str
    publisher: str
    verified_at: str
    effective_from: Optional[str]
    effective_until: Optional[str]
    authority: str
    content_hash: str
    embedding_model: str
    score: float = 0.0
    vector_relevance: float = 0.0
    bm25_score: float = 0.0


@dataclass(frozen=True)
class KnowledgeSearchResult:
    """一次 BGE/BM25 混合检索的候选与耗时。"""

    chunks: List[KnowledgeChunk]
    vector_candidates: int
    bm25_candidates: int
    elapsed_ms: float


@dataclass(frozen=True)
class KnowledgeSourceRef:
    """证据包中的服务层来源引用。"""

    id: str
    chunk_id: str
    title: str
    section: str
    publisher: str
    source_url: str
    verified_at: str
    category: str


@dataclass
class KnowledgeResult:
    """与 Web 业务解耦的 RAG 结果。"""

    used: bool = False
    degraded: bool = False
    status: str = "disabled"
    reason: str = ""
    evidence: str = ""
    sources: List[KnowledgeSourceRef] = field(default_factory=list)
    chunks: List[KnowledgeChunk] = field(default_factory=list)
    elapsed_ms: float = 0.0
    vector_candidates: int = 0
    bm25_candidates: int = 0
