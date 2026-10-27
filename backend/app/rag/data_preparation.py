"""C8 风格的数据准备：JSONL -> Markdown 父文档 -> 标题结构切片。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter

from .embeddings import resolve_local_model


def normalize_text(text: str) -> str:
    """统一换行、空白和不可见控制字符。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def content_hash(text: str) -> str:
    """生成稳定内容哈希。"""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class TravelKnowledgePreparation:
    """加载旅行知识，并用 Markdown 标题结构分块。"""

    HEADERS_TO_SPLIT_ON = [
        ("#", "主标题"),
        ("##", "二级标题"),
        ("###", "三级标题"),
    ]

    def __init__(self, data_path: str | Path):
        self.data_path = Path(data_path)
        self.documents: List[Document] = []
        self.chunks: List[Document] = []
        self.parent_child_map: Dict[str, str] = {}

    def load_documents(self) -> List[Document]:
        """读取 JSONL 知识文档并构造 Markdown 父文档。"""
        if not self.data_path.exists():
            raise FileNotFoundError(f"知识文件不存在: {self.data_path}")

        documents: List[Document] = []
        with self.data_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"{self.data_path}:{line_number} 不是合法 JSON"
                    ) from exc
                if not isinstance(raw, dict):
                    raise ValueError(
                        f"{self.data_path}:{line_number} 必须是 JSON 对象"
                    )
                documents.append(self._document_from_payload(raw))

        self.documents = documents
        return documents

    def _document_from_payload(self, raw: Mapping[str, Any]) -> Document:
        doc_id = str(raw.get("doc_id") or "").strip()
        title = str(raw.get("title") or "").strip()
        city = str(raw.get("city") or "").strip()
        content = normalize_text(str(raw.get("content") or ""))
        if not doc_id or not title or not city or not content:
            raise ValueError("知识文档必须包含 doc_id/title/city/content")

        section = str(raw.get("section") or "正文").strip()
        parent_id = hashlib.md5(doc_id.encode("utf-8")).hexdigest()
        markdown = f"# {title}\n\n## {section}\n\n{content}"
        metadata = {
            "source": str(raw.get("source_url") or self.data_path),
            "source_url": str(raw.get("source_url") or ""),
            "parent_id": parent_id,
            "doc_id": doc_id,
            "doc_type": "parent",
            "title": title,
            "city": city,
            "category": str(raw.get("category") or ""),
            "publisher": str(raw.get("publisher") or ""),
            "verified_at": str(raw.get("verified_at") or ""),
            "effective_from": str(raw.get("effective_from") or ""),
            "effective_until": str(raw.get("effective_until") or ""),
            "authority": str(raw.get("authority") or "curated"),
            "content_hash": content_hash(
                "\n".join(
                    [
                        doc_id,
                        title,
                        city,
                        content,
                        str(raw.get("source_url") or ""),
                        str(raw.get("verified_at") or ""),
                    ]
                )
            ),
            "embedding_model": (
                str(raw.get("embedding_model") or "")
                or resolve_local_model()
            ),
        }
        return Document(page_content=markdown, metadata=metadata)

    def chunk_documents(self) -> List[Document]:
        """使用 C8 的 MarkdownHeaderTextSplitter 进行结构感知切块。"""
        if not self.documents:
            self.load_documents()

        splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=self.HEADERS_TO_SPLIT_ON,
            strip_headers=False,
        )
        all_chunks: List[Document] = []
        for parent in self.documents:
            markdown_chunks = splitter.split_text(parent.page_content)
            parent_id = str(parent.metadata["parent_id"])
            for index, chunk in enumerate(markdown_chunks):
                chunk_id = str(uuid.uuid4())
                chunk.metadata.update(parent.metadata)
                chunk.metadata.update(
                    {
                        "chunk_id": chunk_id,
                        "parent_id": parent_id,
                        "doc_type": "child",
                        "chunk_index": index,
                        "batch_index": len(all_chunks),
                        "chunk_size": len(chunk.page_content),
                        "section": (
                            chunk.metadata.get("二级标题")
                            or chunk.metadata.get("三级标题")
                            or "正文"
                        ),
                    }
                )
                self.parent_child_map[chunk_id] = parent_id
                all_chunks.append(chunk)

        self.chunks = all_chunks
        return all_chunks

    def get_statistics(self) -> Dict[str, Any]:
        """返回父文档、切片和城市统计。"""
        cities: Dict[str, int] = {}
        for doc in self.documents:
            city = str(doc.metadata.get("city") or "未知")
            cities[city] = cities.get(city, 0) + 1
        return {
            "documents": len(self.documents),
            "chunks": len(self.chunks),
            "cities": cities,
            "avg_chunk_size": (
                sum(
                    int(chunk.metadata.get("chunk_size", 0))
                    for chunk in self.chunks
                )
                / len(self.chunks)
                if self.chunks
                else 0
            ),
        }

    def get_parent_documents(
        self,
        child_chunks: Sequence[Document],
    ) -> List[Document]:
        """按命中次数返回去重后的父文档。"""
        relevance: Dict[str, int] = {}
        by_parent = {
            str(doc.metadata.get("parent_id")): doc
            for doc in self.documents
        }
        for chunk in child_chunks:
            parent_id = str(chunk.metadata.get("parent_id") or "")
            if parent_id:
                relevance[parent_id] = relevance.get(parent_id, 0) + 1
        return [
            by_parent[parent_id]
            for parent_id in sorted(
                relevance,
                key=lambda item: relevance[item],
                reverse=True,
            )
            if parent_id in by_parent
        ]
