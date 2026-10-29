# 智能旅行规划助手(trip_planner)

一个可运行的全栈 AI 旅行规划应用。用户输入城市、日期和偏好，系统实时检索景点、天气、酒店和公共旅行知识，再通过一次结构化模型调用生成每日行程、预算、地图和可导出的计划。

后端采用 **FastAPI + LangChain + LangGraph**，前端采用 **Vue 3 + Vite + Ant Design Vue**。

## 功能

- 表单规划与自然语言聊天两个入口，共用同一条检索、记忆和规划流水线。
- SSE 实时推送检索和生成进度，避免几十秒等待期间没有反馈。
- 省份+城市名称规范化、必去景点硬约束、亲子/步行场景的近郊过滤。
- 高德 MCP 提供景点、天气和酒店实时数据；坐标与详情由服务层确定性回填。
- BGE + FAISS + 中文 BM25 + RRF 混合知识检索，返回可核验来源并支持无答案拒答。
- 账号注册/登录、Bearer 会话、登录限流、用户模型配额和账号生命周期管理。
- 会话内改行程、长期偏好、历史行程浏览/详情/删除。
- 模型异常、上游超时和 MCP 故障时返回明确的降级状态，不伪造成功结果。
- 景点图片优先使用 Wikimedia Commons，网络失败时前端保留本地示意图。

## 架构

```text
POST /api/trip/plan         表单直通，一次性返回完整结果
POST /api/trip/plan/stream  表单直通，SSE 推送进度
POST /api/chat/stream       聊天入口，先判意图/抽槽位，再复用规划流水线

LangGraph
  ├─ search_attractions ─┐
  ├─ query_weather      ─┼─> generate_plan
  ├─ search_hotels      ─┤
  └─ search_knowledge   ─┘

景点/天气/酒店: 高德 MCP，确定性调用，不消耗模型额度
知识检索:      BGE + FAISS + BM25 + RRF，不消耗模型额度
行程生成:      TripPlan 结构化输出，一次模型调用
失败处理:      核心检索/规划失败 -> degraded；RAG 失败 -> knowledge_degraded
```

## 本地启动

环境要求：Python 3.13、Node.js 22+、`uvx`、高德 API Key 和 OpenAI 兼容模型 API Key。

```powershell
Copy-Item backend\.env.example backend\.env
Copy-Item frontend\.env.example frontend\.env

python -m pip install -r backend\requirements-dev.txt
Set-Location frontend
npm ci
Set-Location ..

powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
```

本地地址：

- 前端：<http://localhost:5173>
- 后端文档：<http://localhost:8000/docs>

前端默认使用同源 `/api`：开发环境由 Vite 代理到 `localhost:8000`，Docker 生产镜像由 Nginx 代理到后端容器。

## Docker 部署

### 1. 准备配置

```powershell
Copy-Item .env.compose.example .env
Copy-Item backend\.env.example backend\.env
```

必须填写：

- `backend/.env` 中的 `AMAP_API_KEY`
- `backend/.env` 中的 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL_ID`
- 根目录 `.env` 中的 `VITE_AMAP_WEB_JS_KEY`，必须使用高德 **Web 端 JS API Key**

Compose 默认关闭公开注册。需要开放注册时，在根目录 `.env` 中设置 `AUTH_ALLOW_REGISTRATION=true`，同时配置高熵 `AUTH_REGISTRATION_KEY`。

### 2. 启动

```powershell
docker compose up --build -d
docker compose ps
Invoke-RestMethod http://localhost:8080/health
```

默认入口为 <http://localhost:8080>，后端 API 仅绑定到宿主机 `127.0.0.1:${BACKEND_PORT}`，不应直接暴露到公网。

首次启动可能需要下载 Python 依赖中的 embedding 模型以及 `uvx` 缓存的 MCP Server。开始规划前可先注册账号并完成一次健康检查。

### 3. 数据持久化

Compose 使用两个命名卷：

- `trip_data`：SQLite 账号、会话、偏好、历史、配额和 FAISS 索引。
- `rag_models`：HuggingFace / Sentence Transformers 模型缓存。

更新镜像不会删除这两个卷。清理数据前必须先备份：

```powershell
docker run --rm -v trip_planner_trip_data:/data -v ${PWD}:/backup alpine `
  tar czf /backup/trip_data_backup.tar.gz -C /data .
```

### 4. 查看日志与停止

```powershell
docker compose logs -f --tail=200 backend
docker compose logs -f --tail=200 frontend
docker compose down
```

`docker compose down -v` 会连同账号、历史和模型缓存一起删除，除测试环境外不要使用。

### 5. 公网部署

- 在 Compose 前增加 Caddy、Traefik 或云负载均衡，由它处理 HTTPS；不要把后端端口直接暴露到公网。
- 将根目录 `.env` 的 `PUBLIC_ORIGIN` 改成实际 HTTPS 来源。
- 生产环境关闭匿名注册，或配置邀请码；`AUTH_ADMIN_KEY` 至少使用 32 字符高熵随机值。
- 当前未实现 Request ID/结构化日志和全局规划并发背压，公开 Demo 应设置平台级并发与费用告警。

## 关键配置

完整变量说明见 `backend/.env.example`。

| 变量 | 作用 |
| --- | --- |
| `LLM_MODEL_ID` / `LLM_BASE_URL` / `LLM_API_KEY` | OpenAI 兼容模型配置 |
| `AMAP_API_KEY` | 高德地图后端 Key |
| `VITE_AMAP_WEB_JS_KEY` | 高德 Web 端 JS Key |
| `AUTH_ENABLED` | 是否要求 Bearer token |
| `AUTH_ALLOW_REGISTRATION` / `AUTH_REGISTRATION_KEY` | 注册开关与邀请码 |
| `AUTH_ADMIN_KEY` | 账号管理接口密钥，生产环境至少 32 字符 |
| `MODEL_QUOTA_DAILY_CALL_LIMIT` | 每用户每日模型调用单位上限 |
| `RAG_ENABLED` / `RAG_AUTO_BOOTSTRAP` | 知识检索与索引自动构建 |
| `MEMORY_DB_PATH` | 会话和长期偏好的 SQLite 路径 |

## 主要接口

启动后可访问后端直连的 <http://localhost:8000/docs>；`BACKEND_PORT` 改动时请同步替换端口。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 进程存活检查 |
| POST | `/api/auth/register` | 注册并获取 Bearer token |
| POST | `/api/auth/login` | 登录 |
| POST | `/api/trip/plan` | 非流式生成旅行计划 |
| POST | `/api/trip/plan/stream` | SSE 流式生成旅行计划 |
| POST | `/api/chat/stream` | 聊天式规划 |
| GET | `/api/history` | 当前用户历史行程 |
| GET/DELETE | `/api/memory/...` | 长期偏好读取与删除 |
| GET | `/api/poi/photo` | Wikimedia 景点图片 |

业务接口默认需要：

```http
Authorization: Bearer <token>
```

## 测试与 CI

后端离线测试不调用模型、不需要 API Key：

```powershell
Set-Location backend
python -X utf8 -m pytest -q
```

前端类型检查与构建：

```powershell
Set-Location frontend
npm run build
```

RAG 离线检索评测：

```powershell
Set-Location backend
python -X utf8 scripts\eval_rag.py
```

当前自建评测集包含 38 题，用于回归而非行业基准。规划质量评测见 `backend/evals/README.md`；真实 live 评测会消耗模型额度，必须显式确认。

GitHub Actions 位于 `.github/workflows/ci.yml`，在 push 和 pull request 时执行后端测试、前端构建和 Compose 配置校验。

## 目录结构

```text
trip_planner/
├─ backend/
│  ├─ app/agents/              LangGraph 编排与结构化规划
│  ├─ app/api/routes/          鉴权、行程、聊天、历史、记忆和图片接口
│  ├─ app/rag/                 BGE + FAISS + BM25 + RRF
│  ├─ app/services/            高德 MCP、记忆、配额、图片等业务服务
│  ├─ tests/                   离线测试
│  └─ Dockerfile
├─ frontend/
│  ├─ src/                     Vue 页面、组件和服务
│  ├─ Dockerfile
│  └─ nginx.conf
├─ .github/workflows/ci.yml
├─ docker-compose.yml
└─ .env.compose.example
```

## 已知限制

- 规划仍依赖一次耗时约 20~30 秒的模型调用，端到端通常需要 40~60 秒；SSE 改善的是等待体验，不缩短模型生成时间。
- 公共知识库当前是精选种子数据，需要按真实业务需求持续补充人工核验资料，并在资料或 embedding 变化后重跑检索评测。
- 尚未实现 Request ID、结构化日志、外部追踪和全局规划并发背压；公开部署前应在反向代理或平台层增加限流和费用告警。
- SQLite 适合单实例自托管和作品演示；多实例部署前需要迁移到共享数据库，并重新设计会话、配额和索引存储。
- 项目没有多实例高可用与负载测试数据，不应宣称已经达到生产级高并发能力。
