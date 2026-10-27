# 旅行规划质量评测

评测模块不进入线上规划链路，也不新增模型调用。它用固定 golden cases 检查：

- 响应是否成功、是否降级；
- `TripPlan` schema、城市、日期和天数是否一致；
- 每日是否有景点、唯一景点数量是否足够；
- 用户点名地点是否进入行程；
- 亲子、休闲、少走路场景下的城市中心距离限制；
- 同一天景点之间的路线跨度；
- 坐标、预算、占位内容和总体建议是否有效；
- 端到端耗时是否超过 case 上限。

## 命令

```powershell
# 查看案例
& 'E:\hello agents_demo\.venv\Scripts\python.exe' -X utf8 scripts\eval_trip_plans.py --list-cases

# 回放已有响应(不花额度)
& 'E:\hello agents_demo\.venv\Scripts\python.exe' -X utf8 scripts\eval_trip_plans.py --mode replay --input evals\fixtures\sample_replay.json

# 真实调用后端;必须同时给出 --yes-live,且建议先 --limit 3
$env:TRIP_AUTH_USERNAME='eval-user'
$env:TRIP_AUTH_PASSWORD='eval-password'
& 'E:\hello agents_demo\.venv\Scripts\python.exe' -X utf8 scripts\eval_trip_plans.py --mode live --yes-live --limit 3
```

`live` 模式使用后端现有 `/api/trip/plan/stream`，每个 case 会消耗一次规划模型调用。
脚本仍建议使用专用评测账号，并会在每个 case 前清空该账号的长期偏好，避免上一组
“亲子 / 少走路”等偏好污染下一组。需要保留账号记忆做人工观察时，可显式传入
`--keep-evaluation-memory`。脚本不会传 `session_id`，因此不会写入新的历史行程。

报告默认写入 `evals/results/latest.json` 和 `evals/results/latest.md`。

## RAG 检索评测

```powershell
& 'E:\hello agents_demo\.venv\Scripts\python.exe' -X utf8 scripts\eval_rag.py
```

该命令使用本地 BGE/FAISS/BM25,不调用生成模型。评测集位于
`knowledge/eval_questions.jsonl`,统计 `Hit@5`、`MRR@5`、无答案误命中率、
城市串库率和检索延迟,报告写入 `evals/results/rag/`。

需要设置质量门禁时,可显式传入阈值:

```powershell
& 'E:\hello agents_demo\.venv\Scripts\python.exe' -X utf8 scripts\eval_rag.py `
  --min-hit-at-5 1.0 --max-no-answer-false-positive-rate 0.0 --max-p95-ms 1500
```
