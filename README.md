# 智能旅行规划助手(trip_planner)

输入城市、日期和偏好,自动检索真实的景点 / 天气 / 酒店数据,再由 AI 汇总成一份结构化的旅行计划(含每日行程、预算、地图和 PDF 导出)。

后端用 **FastAPI + LangChain + LangGraph**,前端用 **Vue 3 + Vite + Ant Design Vue**。

---

## 1. 它是怎么工作的

一次 `/api/trip/plan` 请求会在 LangGraph 里跑这么一张图:

```
START ─┬─> search_attractions ─┐
       ├─> query_weather ──────┼─> generate_plan ─> END
       └─> search_hotels ──────┘
```

- 前三个节点并行执行,都是**确定性**调用:直接调高德 MCP 工具 + 服务层解析,不经过模型(坐标来自 `maps_search_detail`,不是模型编的);
- `generate_plan` 是**整条链路唯一的模型调用**:把三路结果汇总,用**结构化输出**直接产出 `TripPlan`(模型不支持时退回"纯文本 + JSON 解析");
- 高德数据全部通过 MCP Server(`uvx amap-mcp-server`)获取,**不依赖模型记忆**。

如果规划失败,后端**不会**假装成功:它仍会返回一份可渲染的占位行程,但会把 `degraded` 置为 `true`,前端顶部显示警示条。

---

## 2. 环境要求

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.11+(已在 3.13 上验证) | 后端运行环境 |
| Node.js | 18+ | 前端构建与开发服务器 |
| uv / uvx | 随 `requirements.txt` 一起安装 | 用于拉起高德 MCP Server,缺了就只能拿到降级结果 |
| 高德地图 API Key | 必填 | [控制台申请](https://console.amap.com/dev/key/app) |
| 模型 API Key | 必填 | 默认走 ModelScope 的 OpenAI 兼容接口 |

---

## 3. 快速开始(五步)

```powershell
# 1. 进入项目
cd trip_planner

# 2. 建虚拟环境并安装后端依赖(已装好的话跳过)
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt

# 3. 准备配置:复制模板后填入真实密钥
Copy-Item backend\.env.example  backend\.env
Copy-Item frontend\.env.example frontend\.env

# 4. 装前端依赖
cd frontend; npm install; cd ..

# 5. 一键启动前后端
powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
```

启动后:

- 前端 <http://localhost:5173>
- 后端接口文档 <http://localhost:8000/docs>

`scripts/dev.ps1` 会自动挑选可用的 Python(优先 `backend/venv`,其次仓库上一级的 `.venv`),前端 `node_modules` 缺失时自动 `npm install`,并在你按 Ctrl+C 时停掉子进程。

macOS / Linux 把第 5 步换成两句话即可:

```bash
(cd backend && python run.py) &
(cd frontend && npm run dev)
```

---

## 4. 配置说明

### `backend/.env`

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `LLM_API_KEY` | 是 | 模型密钥;留空时回退读 `MODELSCOPE_API_KEY` |
| `LLM_MODEL_ID` | 是 | 模型名,如 `Qwen/Qwen3.5-27B` |
| `LLM_BASE_URL` | 是 | OpenAI 兼容的接口地址 |
| `LLM_TIMEOUT` | 否 | 单次模型请求超时(秒),默认 60 |
| `AMAP_API_KEY` | 是 | 高德 Key,**缺失会直接导致后端启动失败** |
| `APP_ENV` | 否 | `development`(默认,开热重载)/ `production`(关热重载) |
| `HOST` / `PORT` | 否 | 默认 `0.0.0.0:8000` |
| `CORS_ORIGINS` | 否 | 允许的前端来源,逗号分隔 |
| `LOG_LEVEL` | 否 | uvicorn 日志级别,默认 `INFO` |
| `UNSPLASH_ACCESS_KEY` | 否 | 不配则景点配图接口返回空 |
| `AMAP_MCP_COMMAND` / `AMAP_MCP_ARGS` | 否 | 自定义 MCP 启动命令,默认 `uvx amap-mcp-server` |

### `frontend/.env`

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `VITE_API_BASE_URL` | 是 | 后端地址,默认 `http://localhost:8000` |
| `VITE_AMAP_WEB_JS_KEY` | 是 | 高德 **Web端(JS API)** Key,用于渲染景点地图 |

> `.env` 已被 `.gitignore` 忽略,仓库里只提交 `.env.example`。**不要**把真实密钥提交上去。

---

## 5. 接口一览

启动后可访问 <http://localhost:8000/docs> 自助调试。主要接口:

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 进程存活检查 |
| GET | `/api/trip/health` | 规划服务运行时信息(模型、图状态)。**未配置 LLM Key 时返回 503** |
| POST | `/api/trip/plan` | 生成旅行计划 |
| GET | `/api/map/poi` | POI 搜索 |
| GET | `/api/map/weather` | 天气查询 |
| POST | `/api/map/route` | 路线规划 |
| GET | `/api/map/health` | 高德 Key 是否配置 |
| GET | `/api/poi/detail/{poi_id}` | POI 详情 |
| GET | `/api/poi/photo` | 景点配图(Unsplash,未配置时为空) |

### 生成一份计划

```bash
curl -X POST http://localhost:8000/api/trip/plan \
  -H "Content-Type: application/json" \
  -d '{
    "city": "北京",
    "start_date": "2025-06-01",
    "end_date": "2025-06-03",
    "travel_days": 3,
    "transportation": "公共交通",
    "accommodation": "经济型酒店",
    "preferences": ["历史文化", "美食"],
    "free_text_input": "希望多安排一些博物馆"
  }'
```

注意 `travel_days` 必须等于 `end_date - start_date + 1`,否则直接返回 `422`。

响应(截断):

```json
{
  "success": true,
  "degraded": false,
  "message": "旅行计划生成成功",
  "data": {
    "city": "北京",
    "start_date": "2025-06-01",
    "end_date": "2025-06-03",
    "days": [{ "date": "2025-06-01", "day_index": 0, "attractions": [], "meals": [] }],
    "weather_info": [],
    "overall_suggestions": "...",
    "budget": { "total": 2060 }
  }
}
```

### `degraded` 是什么

| 字段 | 含义 |
| --- | --- |
| `degraded: false` | 真实规划结果 |
| `degraded: true` | AI 规划失败,`data` 是占位行程(景点名形如"北京景点1"),仅供前端渲染结构,**不要直接使用** |

此时 `message` 会说明原因,前端首页弹警告、结果页顶部显示警示条。

---

## 6. 测试

后端(纯离线,不需要 API Key,不会发起网络请求):

```powershell
cd backend
..\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
..\.venv\Scripts\python.exe -m pytest
```

上面假设虚拟环境建在 `trip_planner\.venv`;若建在仓库上一级,把 `..\.venv` 换成 `..\..\.venv`。也可以直接 `cd backend` 后运行 `pytest`(已装进环境的话)。

覆盖范围:JSON/MCP 返回值解析、高德 POI/天气/路线解析、`/plan` 与 `/health` 的 HTTP 契约与请求校验、降级标记逻辑。

前端(类型检查 + 构建,当 CI 最低门槛):

```bash
cd frontend && npm run build
```

---

## 7. 常见问题

**启动报 "配置错误: AMAP_API_KEY未配置"**
后端启动时会校验配置,请先在 `backend/.env` 里填高德 Key。

**`/api/trip/health` 返回 503**
说明 LLM 密钥没配好,检查 `LLM_API_KEY`(或 `MODELSCOPE_API_KEY`)。

**第一次生成计划特别慢(几十秒到两分钟)**
首次请求要现场拉起 MCP 子进程并编译 LangGraph,期间要跑 4 次模型调用和多次高德工具调用。前端超时是 120 秒。后续请求会复用已编译的图,快一些。

**返回的行程里景点叫"北京景点1"**
这是 `degraded=true` 的占位结果。常见原因:高德 Key 无效/额度用尽、ModelScope 限流、`uvx` 不可用。请查看后端控制台日志里的 `⚠️ 使用兜底占位行程(...)` 一行确认原因。

**`uvx` 找不到**
执行 `pip install -r backend/requirements.txt` 会一并安装 `uv`;确认 `<venv>\Scripts\uvx.exe` 存在,或设置 `AMAP_MCP_COMMAND` 指定完整路径。

**前端地图空白**
`frontend/.env` 里的 `VITE_AMAP_WEB_JS_KEY` 必须是**Web端(JS API)**类型的 Key,与后端那个 Key 不是一回事。

**端口被占用**
后端默认 8000、前端 5173、常驻的高德 MCP Server 默认 8001。改 `backend/.env` 的 `PORT`,前端改 `vite.config.ts` 的 `server.port` 与代理目标。

---

## 8. 目录结构

```
trip_planner/
├─ backend/
│  ├─ app/
│  │  ├─ agents/trip_planner_agent.py   # LangGraph 多智能体编排(核心)
│  │  ├─ api/main.py                    # FastAPI 应用与 lifespan
│  │  ├─ api/routes/                    # trip / map / poi 路由
│  │  ├─ services/                      # LLM、高德 MCP、Unsplash
│  │  ├─ models/schemas.py              # Pydantic 请求/响应模型
│  │  ├─ utils/json_utils.py            # LLM / MCP 返回值解析
│  │  └─ config.py                      # 配置与环境变量
│  ├─ tests/                            # pytest(离线可跑)
│  ├─ requirements.txt                  # 已锁定版本
│  ├─ run.py                            # 开发启动入口
│  └─ AGENTS.md                         # 工作交接文档(每次开工前先读)
├─ frontend/                            # Vue 3 + Vite
├─ README.md                            # 本文件
└─ scripts/dev.ps1                      # 一键启动前后端
```

---

## 9. 已知限制

- 生产部署(容器化、限流、鉴权、结构化日志)尚未完成,待办清单见 `backend/AGENTS.md` 第 5 节。
- 一次 `/plan` 只需 1 次模型调用,但这一次单调用就要 20~30 秒(模型侧延迟),整体仍在 1 分钟量级;想再压下去要靠缓存或流式输出(见 `backend/AGENTS.md` 第 5 节)。
- 没有多轮对话与长期偏好记忆;每次请求都是独立规划。
- 高德 MCP Server 已改为常驻 streamable-http(不再每次调用起进程);当前为单实例,高并发前应先压测。
