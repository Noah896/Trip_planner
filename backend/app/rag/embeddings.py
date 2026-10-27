"""C8 同款 HuggingFace embedding 初始化。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from langchain_core.embeddings import Embeddings


DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"


def resolve_local_model(
    model_name: str = DEFAULT_EMBEDDING_MODEL,
    *,
    model_root: Optional[str | Path] = None,
) -> str:
    """优先解析本地模型目录，避免离线环境反复联网校验。

    查找顺序与 C8 一致，并增加 ``RAG_MODEL_ROOT`` 和用户给出的
    ``E:/all-in-rag/models`` 兼容路径。
    """
    if Path(model_name).expanduser().exists():
        return str(Path(model_name).expanduser().resolve())

    short_name = model_name.rsplit("/", 1)[-1]
    backend_root = Path(__file__).resolve().parents[2]
    candidates = []
    if model_root:
        candidates.append(Path(model_root) / short_name)
    if os.getenv("RAG_MODEL_ROOT"):
        candidates.append(Path(os.environ["RAG_MODEL_ROOT"]) / short_name)
    candidates.extend(
        [
            backend_root / "models" / short_name,
            backend_root.parent / "models" / short_name,
            Path("E:/all-in-rag/models") / short_name,
        ]
    )

    for candidate in candidates:
        try:
            candidate = candidate.resolve()
        except OSError:
            continue
        if (candidate / "config.json").exists() and (
            (candidate / "model.safetensors").exists()
            or (candidate / "pytorch_model.bin").exists()
        ):
            return str(candidate)
    return model_name


def create_huggingface_embeddings(
    model_name: str = DEFAULT_EMBEDDING_MODEL,
    *,
    device: str = "cpu",
    model_root: Optional[str | Path] = None,
) -> Embeddings:
    """按 C8 参数创建 BGE embedding：CPU + 归一化向量。"""
    resolved = resolve_local_model(model_name, model_root=model_root)
    if Path(resolved).exists():
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    # 延迟到真正建索引时再导入，避免只做静态检查时加载 PyTorch。
    from langchain_huggingface import HuggingFaceEmbeddings

    return HuggingFaceEmbeddings(
        model_name=resolved,
        model_kwargs={"device": device},
        encode_kwargs={"normalize_embeddings": True},
    )
