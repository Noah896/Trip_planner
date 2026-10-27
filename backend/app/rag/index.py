"""C8 同款 FAISS 索引构建与持久化。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from .embeddings import create_huggingface_embeddings


class KnowledgeIndex:
    """负责 BGE 向量化、FAISS 建库、增量添加和本地持久化。"""

    def __init__(
        self,
        index_save_path: str | Path,
        *,
        model_name: str,
        device: str = "cpu",
        embeddings: Optional[Embeddings] = None,
        model_root: Optional[str | Path] = None,
    ):
        self.index_save_path = Path(index_save_path)
        self.model_name = model_name
        self.device = device
        self.model_root = model_root
        self.embeddings = embeddings
        self.vectorstore: Optional[FAISS] = None

    def setup_embeddings(self) -> Embeddings:
        """按需加载 BGE，测试可注入轻量 fake embeddings。"""
        if self.embeddings is None:
            self.embeddings = create_huggingface_embeddings(
                self.model_name,
                device=self.device,
                model_root=self.model_root,
            )
        return self.embeddings

    def build_vector_index(self, chunks: List[Document]) -> FAISS:
        """从切片构建 FAISS 向量存储。"""
        if not chunks:
            raise ValueError("文档块列表不能为空")
        embeddings = self.setup_embeddings()
        self.vectorstore = FAISS.from_documents(
            documents=chunks,
            embedding=embeddings,
        )
        return self.vectorstore

    def add_documents(self, new_chunks: List[Document]) -> None:
        """向现有 FAISS 索引添加文档。"""
        if self.vectorstore is None:
            raise ValueError("请先构建或加载向量索引")
        self.vectorstore.add_documents(new_chunks)

    def save_index(self) -> None:
        """保存为 LangChain FAISS 标准的 index.faiss/index.pkl。"""
        if self.vectorstore is None:
            raise ValueError("请先构建向量索引")
        self.index_save_path.mkdir(parents=True, exist_ok=True)
        self.vectorstore.save_local(str(self.index_save_path))

    def load_index(self) -> Optional[FAISS]:
        """加载本地 FAISS；目录缺失或损坏时返回 None。"""
        faiss_file = self.index_save_path / "index.faiss"
        pkl_file = self.index_save_path / "index.pkl"
        if not faiss_file.exists() or not pkl_file.exists():
            return None
        embeddings = self.setup_embeddings()
        try:
            self.vectorstore = FAISS.load_local(
                str(self.index_save_path),
                embeddings,
                allow_dangerous_deserialization=True,
            )
            return self.vectorstore
        except Exception:
            self.vectorstore = None
            return None

    def similarity_search(self, query: str, k: int = 5) -> List[Document]:
        """直接执行 FAISS 相似度检索。"""
        if self.vectorstore is None:
            raise ValueError("请先构建或加载向量索引")
        return self.vectorstore.similarity_search(query, k=k)

    def stats(self) -> dict[str, Any]:
        """返回持久化文件统计。"""
        return {
            "index_path": str(self.index_save_path),
            "index_exists": (self.index_save_path / "index.faiss").exists()
            and (self.index_save_path / "index.pkl").exists(),
            "vector_count": (
                int(self.vectorstore.index.ntotal)
                if self.vectorstore is not None
                else None
            ),
            "model": self.model_name,
            "device": self.device,
        }
