"""C8 同款 BM25 + FAISS 混合召回与 RRF 融合。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from langchain_community.retrievers import BM25Retriever
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document


@dataclass(frozen=True)
class HybridSearchResult:
    """混合检索结果及两条召回通道的候选数。"""

    documents: List[Document]
    vector_documents: List[Document]
    bm25_documents: List[Document]


def chinese_preprocessing_func(text: str) -> List[str]:
    """用中文字符二元组和英文数字词支持无分词依赖的 BM25。"""
    tokens: List[str] = []
    for segment in re.findall(
        r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+",
        (text or "").casefold(),
    ):
        if re.fullmatch(r"[\u4e00-\u9fff]+", segment):
            if len(segment) == 1:
                tokens.append(segment)
            else:
                tokens.extend(
                    segment[index : index + 2]
                    for index in range(len(segment) - 1)
                )
        else:
            tokens.append(segment)
    return tokens or [" "]


class HybridRetriever:
    """与 C8 一致的向量/BM25 双路召回和 metadata 过滤。"""

    def __init__(
        self,
        vectorstore: FAISS,
        chunks: List[Document],
        *,
        vector_k: int = 5,
        bm25_k: int = 5,
        rrf_k: int = 60,
    ):
        self.vectorstore = vectorstore
        self.chunks = chunks
        self.rrf_k = rrf_k
        self.vector_k = vector_k
        self.bm25_k = bm25_k
        self.vector_retriever = vectorstore.as_retriever(
            search_type="similarity",
            search_kwargs={"k": vector_k},
        )
        self.bm25_retriever = BM25Retriever.from_documents(
            chunks,
            k=bm25_k,
            preprocess_func=chinese_preprocessing_func,
        )

    def search_with_candidates(self, query: str) -> HybridSearchResult:
        """分别执行 FAISS 和 BM25，再进行 RRF。"""
        vector_pairs = self.vectorstore.similarity_search_with_score(
            query,
            k=self.vector_k,
        )
        vector_docs = [
            self._copy_with_metadata(
                doc,
                # 索引 embedding 已归一化,L2 距离 d 满足 cos = 1 - d/2。
                vector_relevance=max(0.0, min(1.0, 1.0 - float(score) / 2.0)),
            )
            for doc, score in vector_pairs
        ]

        processed_query = chinese_preprocessing_func(query)
        raw_bm25_scores = self.bm25_retriever.vectorizer.get_scores(
            processed_query
        )
        bm25_by_content = {
            hashlib.md5(doc.page_content.encode("utf-8")).hexdigest(): float(
                score
            )
            for doc, score in zip(self.chunks, raw_bm25_scores)
        }
        bm25_docs = [
            self._copy_with_metadata(
                doc,
                bm25_score=bm25_by_content.get(
                    hashlib.md5(doc.page_content.encode("utf-8")).hexdigest(),
                    0.0,
                ),
            )
            for doc in self.bm25_retriever.invoke(query)
        ]
        return HybridSearchResult(
            documents=self._rrf_rerank(vector_docs, bm25_docs),
            vector_documents=vector_docs,
            bm25_documents=bm25_docs,
        )

    def hybrid_search(self, query: str, top_k: int = 3) -> List[Document]:
        """返回 RRF 后的前 ``top_k`` 个文档。"""
        return self.search_with_candidates(query).documents[:top_k]

    def metadata_filtered_search(
        self,
        query: str,
        filters: Dict[str, Any],
        top_k: int = 3,
        *,
        predicate: Optional[Callable[[Document], bool]] = None,
        min_vector_relevance: float = 0.0,
        lexical_rescue_min_vector_relevance: float = 0.0,
        lexical_rescue_min_bm25_score: float = 0.0,
    ) -> HybridSearchResult:
        """先混合召回候选，再应用 C8 风格 metadata 精确过滤。"""
        raw = self.search_with_candidates(query)
        filtered: List[Document] = []
        for doc in raw.documents:
            if not self._matches_filters(doc, filters):
                continue
            if predicate is not None and not predicate(doc):
                continue
            if not self.is_relevant(
                doc,
                min_vector_relevance=min_vector_relevance,
                lexical_rescue_min_vector_relevance=(
                    lexical_rescue_min_vector_relevance
                ),
                lexical_rescue_min_bm25_score=(
                    lexical_rescue_min_bm25_score
                ),
            ):
                continue
            filtered.append(doc)
            if len(filtered) >= top_k:
                break
        return HybridSearchResult(
            documents=filtered,
            vector_documents=raw.vector_documents,
            bm25_documents=raw.bm25_documents,
        )

    @staticmethod
    def _matches_filters(doc: Document, filters: Dict[str, Any]) -> bool:
        for key, value in filters.items():
            if key not in doc.metadata:
                return False
            if isinstance(value, list):
                if doc.metadata[key] not in value:
                    return False
            elif doc.metadata[key] != value:
                return False
        return True

    @staticmethod
    def is_relevant(
        doc: Document,
        *,
        min_vector_relevance: float = 0.0,
        lexical_rescue_min_vector_relevance: float = 0.0,
        lexical_rescue_min_bm25_score: float = 0.0,
    ) -> bool:
        """用语义分或“中等语义 + 字词锚点”拒绝无关的同城候选。"""
        if (
            min_vector_relevance <= 0
            and lexical_rescue_min_vector_relevance <= 0
        ):
            return True
        vector_relevance = float(
            doc.metadata.get("vector_relevance") or 0.0
        )
        bm25_score = float(doc.metadata.get("bm25_score") or 0.0)
        if vector_relevance >= min_vector_relevance:
            return True
        return (
            vector_relevance >= lexical_rescue_min_vector_relevance
            and bm25_score >= lexical_rescue_min_bm25_score
        )

    @staticmethod
    def _copy_with_metadata(doc: Document, **values: float) -> Document:
        return Document(
            page_content=doc.page_content,
            metadata={**doc.metadata, **values},
            id=doc.id,
        )

    def _rrf_rerank(
        self,
        vector_docs: Sequence[Document],
        bm25_docs: Sequence[Document],
    ) -> List[Document]:
        """使用 C8 的 reciprocal rank fusion，k 默认 60。"""
        scores: Dict[str, float] = {}
        objects: Dict[str, Document] = {}

        for rank, doc in enumerate(vector_docs):
            doc_id = hashlib.md5(
                doc.page_content.encode("utf-8")
            ).hexdigest()
            objects[doc_id] = self._merge_document(
                objects.get(doc_id),
                doc,
            )
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (
                self.rrf_k + rank + 1
            )

        for rank, doc in enumerate(bm25_docs):
            doc_id = hashlib.md5(
                doc.page_content.encode("utf-8")
            ).hexdigest()
            objects[doc_id] = self._merge_document(
                objects.get(doc_id),
                doc,
            )
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (
                self.rrf_k + rank + 1
            )

        reranked: List[Document] = []
        for doc_id, score in sorted(
            scores.items(),
            key=lambda item: item[1],
            reverse=True,
        ):
            reranked.append(
                self._copy_with_metadata(
                    objects[doc_id],
                    rrf_score=score,
                )
            )
        return reranked

    @staticmethod
    def _merge_document(
        existing: Optional[Document],
        new: Document,
    ) -> Document:
        if existing is None:
            return new
        return Document(
            page_content=new.page_content,
            metadata={**existing.metadata, **new.metadata},
            id=new.id or existing.id,
        )
