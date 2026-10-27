"""独立 ``rag`` 包与旅行规划业务之间的薄适配层。

这里只负责把 ``TripRequest`` 转成通用查询、读取应用配置，并把包内的来源引用
转换为 HTTP 契约使用的 Pydantic 模型。索引、检索、融合和超时逻辑都在
``app/rag/`` 子包内，且该子包不依赖 ``app`` 中的配置、模型或 Web 层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ..rag import (
    KnowledgeConfig,
    KnowledgeService,
    KnowledgeSourceRef,
    format_knowledge_evidence,
)

from ..config import get_settings
from ..models.schemas import KnowledgeSource, TripRequest


BACKEND_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class KnowledgeRetrievalResult:
    """业务层使用的 RAG 结果，包含可直接响应的 Pydantic 来源。"""

    enabled: bool = False
    used: bool = False
    degraded: bool = False
    status: str = "disabled"
    reason: str = ""
    evidence: str = ""
    sources: List[KnowledgeSource] = field(default_factory=list)
    elapsed_ms: float = 0.0
    vector_candidates: int = 0
    bm25_candidates: int = 0


def build_knowledge_query(request: TripRequest) -> str:
    """用城市、偏好、必去景点和自由文本构造确定性查询。"""
    parts = [
        f"城市 {request.city}",
        "旅行 规划",
        *request.preferences,
        *request.must_visit,
        request.free_text_input or "",
    ]
    return " ".join(part.strip() for part in parts if part and part.strip())


def _path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    cwd_path = Path.cwd() / path
    if cwd_path.exists():
        return cwd_path
    return BACKEND_ROOT / path


def _source_from_ref(source: KnowledgeSourceRef) -> KnowledgeSource:
    return KnowledgeSource(
        id=source.id,
        chunk_id=source.chunk_id,
        title=source.title,
        section=source.section,
        publisher=source.publisher,
        source_url=source.source_url,
        verified_at=source.verified_at,
        category=source.category,
    )


_knowledge_service: Optional[KnowledgeService] = None
_knowledge_service_key: Optional[tuple] = None


def get_knowledge_service() -> KnowledgeService:
    """获取当前应用配置对应的独立 RAG 服务单例。"""
    global _knowledge_service, _knowledge_service_key
    settings = get_settings()
    key = (
        str(_path(settings.rag_index_path).resolve()),
        str(_path(settings.rag_seed_path).resolve()),
        bool(settings.rag_auto_bootstrap),
        int(settings.rag_top_k),
        int(settings.rag_vector_k),
        int(settings.rag_bm25_k),
        int(settings.rag_rrf_k),
        float(settings.rag_timeout_seconds),
        int(settings.rag_max_context_chars),
        float(settings.rag_min_vector_relevance),
        float(settings.rag_lexical_rescue_min_vector_relevance),
        float(settings.rag_lexical_rescue_min_bm25_score),
        settings.rag_embedding_model,
        settings.rag_model_root,
        settings.rag_device,
    )
    if _knowledge_service is None or _knowledge_service_key != key:
        _knowledge_service = KnowledgeService(
            KnowledgeConfig(
                index_path=_path(settings.rag_index_path),
                seed_path=_path(settings.rag_seed_path),
                auto_bootstrap=settings.rag_auto_bootstrap,
                top_k=settings.rag_top_k,
                vector_k=settings.rag_vector_k,
                bm25_k=settings.rag_bm25_k,
                rrf_k=settings.rag_rrf_k,
                timeout_seconds=settings.rag_timeout_seconds,
                max_context_chars=settings.rag_max_context_chars,
                min_vector_relevance=settings.rag_min_vector_relevance,
                lexical_rescue_min_vector_relevance=(
                    settings.rag_lexical_rescue_min_vector_relevance
                ),
                lexical_rescue_min_bm25_score=(
                    settings.rag_lexical_rescue_min_bm25_score
                ),
                embedding_model=settings.rag_embedding_model,
                model_root=settings.rag_model_root or None,
                device=settings.rag_device,
            )
        )
        _knowledge_service_key = key
    return _knowledge_service


def reset_knowledge_service() -> None:
    """重置适配层单例，供测试或切换配置时使用。"""
    global _knowledge_service, _knowledge_service_key
    _knowledge_service = None
    _knowledge_service_key = None


def initialize_knowledge_service() -> None:
    """预热 BGE、FAISS 和 BM25，避免首个规划请求承担模型加载时间。"""
    if get_settings().rag_enabled:
        get_knowledge_service().initialize()


# 兼容上一版测试/调用方的名称。
reset_knowledge_index = reset_knowledge_service


async def retrieve_knowledge(request: TripRequest) -> KnowledgeRetrievalResult:
    """调用独立 RAG 包，并把结果映射为业务响应模型。"""
    settings = get_settings()
    if not settings.rag_enabled:
        return KnowledgeRetrievalResult(status="disabled")

    result = await get_knowledge_service().retrieve(
        build_knowledge_query(request),
        city=request.city,
        as_of=request.start_date,
    )
    sources = [_source_from_ref(source) for source in result.sources]

    if result.status == "ok":
        print(
            "📚 知识检索完成:"
            f"命中 {len(sources)} 条,向量候选 {result.vector_candidates},"
            f"BM25 候选 {result.bm25_candidates},耗时 {result.elapsed_ms}ms"
        )
    elif result.status == "timeout":
        print(f"⚠️  {result.reason},继续实时检索与规划")
    elif result.status == "error":
        print(f"⚠️  {result.reason},继续实时检索与规划")
    elif result.status == "no_match":
        print(f"ℹ️  {result.reason},继续实时检索与规划")

    return KnowledgeRetrievalResult(
        enabled=True,
        used=result.used,
        degraded=result.degraded,
        status=result.status,
        reason=result.reason,
        evidence=result.evidence,
        sources=sources,
        elapsed_ms=result.elapsed_ms,
        vector_candidates=result.vector_candidates,
        bm25_candidates=result.bm25_candidates,
    )


__all__ = [
    "KnowledgeRetrievalResult",
    "build_knowledge_query",
    "format_knowledge_evidence",
    "get_knowledge_service",
    "initialize_knowledge_service",
    "reset_knowledge_index",
    "reset_knowledge_service",
    "retrieve_knowledge",
]
