# 公共旅行知识种子库

`seed_documents.jsonl` 是项目内部人工整理的短知识卡，用于演示和测试 RAG
索引链路，不代表实时官方公告。高德仍是景点坐标、天气、酒店和营业状态的
权威来源；预约、开放和交通规则在使用前必须通过 `source_url` 对应的官方渠道
再次核验。

索引、混合检索、证据包和超时逻辑位于 `app/rag/` 子包。实现采用 C8 的
`BAAI/bge-small-zh-v1.5` + FAISS + BM25 + RRF；该子包不依赖 FastAPI、
Pydantic 或其它 `app.*` 业务模块；应用侧只通过
`app/services/knowledge_retrieval.py` 做配置和响应模型适配。

每条文档一行，必需字段：

- `doc_id`、`title`、`city`、`content`、`verified_at`

可选字段：

- `publisher`、`source_url`、`published_at`
- `effective_from`、`effective_until`
- `category`、`authority`、`section`

重建索引：

```powershell
& 'E:\hello agents_demo\.venv\Scripts\python.exe' -X utf8 scripts\ingest_knowledge.py `
  --input knowledge\seed_documents.jsonl --rebuild --stats
```

检查召回：

```powershell
& 'E:\hello agents_demo\.venv\Scripts\python.exe' -X utf8 scripts\check_rag.py `
  --query "故宫周一预约 带六岁孩子" --city 北京 --as-of 2026-11-21
```

运行离线检索质量评测：

```powershell
& 'E:\hello agents_demo\.venv\Scripts\python.exe' -X utf8 scripts\eval_rag.py
```

评测问题位于 `knowledge/eval_questions.jsonl`。脚本会校验预期 `doc_id` 是否存在、
是否属于问题城市,并输出 `Hit@5`、`MRR@5`、无答案误命中率、城市串库率和
P50/P95 延迟。报告写入已忽略版本库的 `evals/results/rag/`。

当前 38 题基线为 22 个有答案、16 个无答案。BGE 高语义分直接接纳;中等语义分
只有在中文 BM25 字词证据足够时才召回。该双门槛针对当前 11 个知识卡校准,
新增文档后必须重跑评测并重新确认阈值。
