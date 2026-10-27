# app.rag

旅行规划后端内的独立 RAG 子包，实现参数对齐
`E:\all-in-rag\code\C8`：

- embedding：`BAAI/bge-small-zh-v1.5`，CPU、归一化向量；
- 索引：LangChain FAISS，持久化为 `index.faiss` + `index.pkl`；
- 切片：`MarkdownHeaderTextSplitter`；
- 召回：FAISS Top 5 + BM25 Top 5，使用 RRF(k=60) 融合；
- 过滤：城市 metadata + 生效/失效日期；
- 拒答：BGE 语义分与中文 BM25 字词分组成双门槛,避免“同城但无关”的候选
  被当作知识证据；
- 业务适配：`app/services/knowledge_retrieval.py`。

`app.rag` 不依赖 FastAPI、Pydantic、`app.config`、`app.models` 或
`app.services`。测试通过 `Embeddings` 注入快速 fake provider；真实 BGE
集成需要显式设置 `RUN_RAG_MODEL_TESTS=1`，避免普通测试反复加载 PyTorch。

离线检索评测使用 `scripts/eval_rag.py` 和 `knowledge/eval_questions.jsonl`。
当前阈值只针对首版 11 个知识卡和 38 个问题校准;资料或 embedding 模型变化后
必须重跑评测。
