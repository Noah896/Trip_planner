# 旅行规划质量评测

评测模块不进入线上规划链路，也不新增模型调用。它用固定 golden cases 检查：

- 响应是否成功、是否降级；
- `TripPlan` schema、城市、日期和天数是否一致；
- 每日是否有景点、唯一景点数量是否足够；
- 用户点名地点是否进入行程；
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
请使用专用评测账号，避免长期偏好污染评测；脚本不会传 `session_id`，因此不会写入
新的历史行程。

报告默认写入 `evals/results/latest.json` 和 `evals/results/latest.md`。
