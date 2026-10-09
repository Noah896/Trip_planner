# trip_planner 后端梳理与后续优化建议

> 阅读范围:`REFACTOR_LANGCHAIN.md` + `trip_planner/backend/**`(重点 `app/`)
> 关联文档:`REFACTOR_LANGCHAIN.md`(记录本次 HelloAgents → LangChain 的重构过程)
> 梳理时间:2026-10-05
> 说明:本文档是"现状梳理 + 问题清单 + 优化路线图",不含具体实现改动。

---

## 一、结论速览

这次重构已经把后端从 `hello-agents` 框架整体切换到了 **LangChain 1.x + LangGraph 1.x**,
并且**对外接口契约零变化**(`POST /api/trip/plan` 的请求/响应、前端 `frontend/src/services/api.ts`
都不用改)。核心成果可以概括为三点:

1. **工具调用从"文本协议"升级为原生 function calling**。提示词里整段 `[TOOL_CALL:...]`
   协议说明和对应的正则解析全部删除,工具由 `create_agent(tools=[...])` 在构造期注入。
2. **编排从"4 个 Agent 串行"升级为 LangGraph 并行 fan-out**。景点 / 天气 / 酒店三路检索
   互不依赖,由 `StateGraph` 并行调度后再汇聚到 `generate_plan`。
3. **输出从"手工抠 JSON"升级为结构化输出**。`planner_agent` 用
   `response_format=TripPlan` 约束 Pydantic 模型,同时保留 `extract_json()` 文本兜底。

这次重构解决的是**框架依赖和调用范式**问题。它**没有**解决下面这些产品级能力:
多轮记忆、知识库检索(RAG)、持久化/断点续跑、流式输出、缓存、鉴权、可观测性、评测。
因此下一阶段的主线,建议从"重构收尾 + 能力补齐"两个方向展开(见第五、六节)。

---

## 二、总体架构

### 2.1 分层结构

```
trip_planner/backend/
├── run.py                        # 启动入口:uvicorn app.api.main:app (reload=True)
├── requirements.txt              # langchain / langgraph / langchain-mcp-adapters / fastapi ...
├── .env                          # 运行配置(LLM / 高德 / Unsplash)
└── app/
    ├── config.py                 # Settings + resolve_llm_credentials() + 校验/打印
    ├── api/
    │   ├── main.py               # FastAPI 应用 + lifespan + CORS + 路由注册
    │   └── routes/
    │       ├── trip.py           # /api/trip/plan、/api/trip/health  ← 主链路
    │       ├── map.py            # /api/map/poi、/weather、/route、/health
    │       └── poi.py            # /api/poi/detail/{id}、/search、/photo
    ├── agents/
    │   └── trip_planner_agent.py # 核心:LangGraph 编排 + 4 个 create_agent + 兜底
    ├── services/
    │   ├── llm_service.py        # ChatOpenAI 单例
    │   ├── amap_mcp.py           # MultiServerMCPClient → LangChain Tool 桥接 + 缓存
    │   ├── amap_service.py       # 高德 MCP 结果的领域化解析(POI/天气/路线/地理编码)
    │   └── unsplash_service.py   # Unsplash 图片(同步 requests,路由里用 to_thread 包装)
    ├── models/
    │   └── schemas.py            # 全部 Pydantic 请求/响应模型(对外契约)
    └── utils/
        └── json_utils.py         # extract_json / parse_mcp_payload / json_dumps
```

### 2.2 主链路时序(生成一次行程)

```
POST /api/trip/plan  (TripRequest)
        │
        ▼
api/routes/trip.py::plan_trip
        │  取单例 agent, await agent.plan_trip(request)
        ▼
MultiAgentTripPlanner.plan_trip
        │  await self._ensure_graph()          ← 首次调用才惰性构建
        │        └─ get_amap_tools()           ← 拉取 16 个高德 MCP 工具(进程内缓存)
        │           create_agent × 4
        │           StateGraph.compile()
        ▼
graph.ainvoke({"request": request})
        ├─ search_attractions ─┐
        ├─ query_weather      ─┼─ 三路并行 fan-out
        ├─ search_hotels      ─┘
        │                      │
        │                      ▼  汇聚
        └─────────────► generate_plan
                                │  优先 response_format=TripPlan
                                │  失败 → planner_fallback + extract_json
                                ▼
                             TripPlan
        │
        ▼
TripPlanResponse(success=True, data=TripPlan)  → 前端
```

任何环节抛异常 → `plan_trip` 捕获 → `_create_fallback_plan()` 返回一份**占位行程**
(`trip_planner_agent.py` 末尾),保证接口 200,而不是 5xx。

### 2.3 各层职责与依赖方向

| 层 | 文件 | 职责 | 依赖 |
| --- | --- | --- | --- |
| 入口 | `run.py` / `api/main.py` | 启停、CORS、路由注册、配置校验 | `config` |
| 路由 | `api/routes/*.py` | 参数校验、响应包装、异常转 HTTP | `agents` / `services` / `models` |
| 编排 | `agents/trip_planner_agent.py` | 多智能体协作、LangGraph、兜底 | `services` / `models` / `utils` |
| 服务 | `services/*.py` | LLM、MCP 桥接、领域解析 | `models` / `utils` / `config` |
| 模型 | `models/schemas.py` | 对外契约 + 内部结构 | — |
| 工具 | `utils/json_utils.py` | 稳健 JSON 抽取 | — |

依赖是单向的(路由 → 编排 → 服务 → 模型),没有循环依赖,分层是干净的。

---

## 三、逐层梳理

### 3.1 入口与生命周期(`api/main.py`、`run.py`)

- 已用 `lifespan` 上下文管理器替代被弃用的 `@app.on_event("startup")`。
- 启动时打印配置并 `validate_config()`:`AMAP_API_KEY` 缺失会**直接阻断启动**,
  LLM Key 缺失只告警(`config.py`)。
- `run.py` 里 `reload=True` 固定开启,这是开发配置;生产部署需要另配。
- CORS 白名单来自 `settings.get_cors_origins_list()`,默认放开 5173 / 3000。

### 3.2 核心编排(`agents/trip_planner_agent.py`)

这是重构的重点,值得逐点说明:

- **惰性构建 + 并发安全**:`_ensure_graph()` 用 `asyncio.Lock()` 做双检锁,
  避免并发首个请求重复构建图;工具与图都推迟到首次 `plan_trip` 才创建,
  目的是不在启动时拉起 `uvx` 子进程。
- **三路并行**:对 `search_attractions` / `query_weather` / `search_hotels`
  各连一条 `START → node → generate_plan` 的边,实现 fan-out + 汇聚。
- **编排用查询拼接而非消息协议**:`_build_attraction_query` / `_build_planner_query`
  把结构化请求渲染成自然语言提示词,交给 Agent。
- **结构化输出双路径**:`_run_structured_planner` 先读
  `result["structured_response"]`,拿不到再退回 `_planner_fallback` + `extract_json()`
  + `TripPlan` 校验。这是对 ModelScope 模型 function calling 支持不完整的稳妥处理。
- **子 Agent 失败不中断整图**:`_run_agent` 捕获异常后返回空串,
  `plan_trip` 里再用 `any(results)` 判断是否需要整体兜底。
- **运行时信息**:`describe()` 提供给 `/api/trip/health`,且刻意不触发工具加载。

### 3.3 工具层(`services/amap_mcp.py`)

- 用 `MultiServerMCPClient` 以 **stdio** 方式启动 `uvx amap-mcp-server`,
  把 MCP 工具直接转成 LangChain `BaseTool`。
- 只缓存"工具清单"(`_tools_cache`),不维护长连接 —— 因为该适配器
  **每次工具调用都会新建一个 MCP 会话**(见 `REFACTOR_LANGCHAIN.md` 踩坑记录 3)。
- `find_tool()` 同时兼容 `maps_text_search` 与 `amap_maps_text_search` 两种命名,
  方便从旧代码迁移。
- `extract_text()` 把 MCP 的 `CallToolResult` / list / dict 归一化成文本。

### 3.4 服务层(`services/amap_service.py`、`llm_service.py`、`unsplash_service.py`)

- `AmapService` 全部方法改为 **async**,并补齐了旧版标 `TODO` 的结果解析:
  - `_parse_pois` 兼容 `pois` / `data`,字段兼容 `typecode`;注意
    `maps_text_search` **不返回经纬度**(`location` 因此改为可选)。
  - `_parse_weather` 同时兼容 `lives[]`、`forecasts[]` 平铺、`forecasts[].casts[]` 嵌套。
  - `plan_route` / `geocode` / `get_poi_detail` 透传高德结构。
- `llm_service.get_llm()` 是 `ChatOpenAI` 单例,凭据由
  `settings.resolve_llm_credentials()` 按 `llm_api_key → modelscope_api_key → 环境变量`
  优先级解析,补齐了 `HelloAgentsLLM` 原本的 provider 推断能力。
- `unsplash_service` 仍是同步 `requests`;路由 `poi.py` 用
  `asyncio.to_thread()` 包装,避免阻塞事件循环。这一层尚未异步化,但影响可控。

### 3.5 数据模型(`models/schemas.py`)

- `TripRequest` / `TripPlan` / `TripPlanResponse` 是**对外契约**,刻意保持不变。
- `TripPlan.days[*]` 结构较完整:每天含 `hotel` / `attractions` / `meals`,
  顶层含 `weather_info` 和可选的 `budget`。
- `POIInfo.location` 由必填改为可选,是对高德文本搜索接口行为的适配。
- `WeatherInfo` 有 `field_validator` 清洗温度单位(把 `25℃` → `25`)。
- 注意:`Budget` 是 `Optional`,而**兜底行程不设置 budget、`weather_info` 也置空**,
  前端展示时需容忍空值。

### 3.6 配置与工具函数(`config.py`、`utils/json_utils.py`)

- `Settings` 用 `pydantic_settings`,并额外兼容加载同级 `HelloAgents/.env`
  (`override=False`),这是重构期的过渡设计,后续可以移除。
- `resolve_llm_credentials()` 是唯一的凭据解析入口,职责清晰。
- `extract_json()` 三级降级(Markdown 代码块 → 全文 → 括号切片),
  `parse_mcp_payload()` 会下钻 `{"content":[{"type":"text",...}]}` 一层。
  这两个函数是当前"抗模型/工具输出不稳定"的关键。

### 3.7 API 契约(供对照)

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/trip/plan` | 主链路,返回 `TripPlanResponse` |
| GET | `/api/trip/health` | 返回 `describe()`,不触发工具加载 |
| GET | `/api/map/poi` `/weather` `/route` `/health` | 高德服务直连接口 |
| GET | `/api/poi/detail/{poi_id}` `/search` `/photo` | POI 详情、搜索、Unsplash 图片 |
| GET | `/` `/health` | 应用级信息与健康检查 |

前端只用到了 `POST /api/trip/plan` 和 `GET /health`,契约与 `frontend/src/types/index.ts`
完全一致。

---

## 四、现状问题与风险清单

下面按"影响面"排序。每条都标注了具体落点,便于后续逐项处理。

### 4.1 安全(最高优先级)

1. **密钥明文入库**:`backend/.env` 里 `MODELSCOPE_API_KEY`、`UNSPLASH_ACCESS_KEY`、
   `UNSPLASH_SECRET_KEY`、`AMAP_API_KEY` 都是真实值,且 `.env` 随目录存在。
   应立即确认它**没有被提交进 git**,并轮换已暴露的 key,
   改用环境变量/密钥管理注入。
2. **无鉴权、无限流**:`/api/trip/plan` 任何人都能调用,直接消耗 LLM 额度。
   至少需要:调用方鉴权(API Key/JWT)、每 IP/每用户的速率限制。
3. **CORS 与调试**:`allow_credentials=True` + 通配方法头,配合 `reload=True`,
   仅适合本地开发。

### 4.2 性能与成本

1. **每次工具调用新起 `uvx` 子进程**:这是 `MultiServerMCPClient` stdio 的固有行为,
   单次几秒,是当前最大延迟来源。可选优化:改常驻 `streamable-http` 传输,
   或直接用 `httpx` 把高德 REST API 封装成普通 LangChain Tool(不绕 MCP)。
2. **无缓存**:完全相同的请求会重新跑 4 次大模型 + 多次工具调用。
   可按 `TripRequest` 规范化哈希做结果缓存(TTL),或对 POI/天气做服务级缓存。
3. **无并发上限**:每个请求会 fan-out 出 3 个 Agent,而每个 Agent 又可能多次调工具。
   高并发下 `uvx` 进程数会迅速膨胀,需要信号量/队列做背压。
4. **冷启动慢**:首次 `/plan` 要加载工具 + 建图。若要优化,可在 `lifespan` 里预热
   (`await get_amap_tools()`),或改成常驻连接。

### 4.3 能力缺口

1. **没有记忆/会话**:`TripRequest` 里没有 `session_id`/`conversation_id`,
   图也没有 `checkpointer`。每次请求都是无状态的,用户无法"在多轮里改行程"
   (例如"把第二天换成博物馆""再加一天")。
2. **没有 RAG/知识库**:所有信息都依赖实时高德工具 + 模型自身知识。
   缺少本地攻略、游记、景点介绍、开放时间、最佳季节等**非结构化知识**的检索。
   `requirements.txt` 里已带 `huggingface_hub`,说明基础设施有预留但未接线。
3. **没有持久化/断点续跑**:`REFACTOR_LANGCHAIN.md` 已提到 LangGraph 支持
   `checkpointer`,当前未接入,无法中断恢复、人工审核(interrupt)、失败重试。
4. **没有流式输出**:`/api/trip/plan` 一次性返回整份计划,前端超时设 120s。
   三路并行 + 规划本身耗时较长,用户在此期间看不到任何进度。
5. **没有多语言/多城市约束**:`_parse_location`、兜底坐标都硬编码了中国大陆范围
   (`116.4/39.9`),国际化场景不成立。

### 4.4 可靠性与正确性

1. **兜底会返回"假数据"**:`_create_fallback_plan` 生成 `"{city}景点1/2"` 和拍脑袋的
   经纬度,且 `weather_info=[]`、无 `budget`。接口层面是"成功",但内容不可用。
   建议兜底至少标记 `success=False` 或加 `degraded` 字段,让前端区分。
2. **静默失败**:`_run_agent` 把异常吞成空串,`plan_trip` 再整体兜底,
   排障时只能靠 `print`。应记录结构化日志并上报。
3. **坐标可能被模型编造**:景点检索用的 `maps_text_search` 不返回经纬度,
   而规划提示词却要求"景点经纬度必须真实准确"。要么强制检索后补
   `maps_search_detail`,要么让规划不做坐标、改由服务层回填。
4. **结构化输出依赖模型能力**:`response_format=TripPlan` 能否生效取决于模型的
   function calling 完整度,当前靠双路径兜底,属于"能跑但不够确定"。
5. **`budget` 常为空**:提示词要求输出预算,但模型未必稳定给出;前端 `budget` 是可选,
   展示需要兜底逻辑。
6. **健康检查不校验真实依赖**:`/api/trip/health` 只看 `describe()`,
   `/api/map/health` 只看 key 是否存在,不实际 ping MCP/LLM。

### 4.5 可维护性

1. **无测试**:没有单元测试/集成测试,重构验证靠 `TestClient` 手工跑。
2. **无结构化日志/追踪**:大量 `print` + emoji,缺少 request id、LangSmith/Langfuse 追踪。
3. **配置有过渡残留**:`config.py` 仍加载 `HelloAgents/.env`,
   `openai_*` 字段保留兼容,`app_name` 已改名 —— 后续可清理。
4. **`requirements.txt` 版本约束偏松**(`>=` 无上界),`langchain 1.x` 生态迭代快,
   建议锁定到具体小版本或用 `constraints.txt`。
5. **未使用依赖**:`python-dateutil`、`huggingface_hub` 目前代码里没有实际引用。

---

## 五、后续优化路线图

建议按"先安全稳定 → 再补能力 → 后提性能"的顺序推进。每阶段都能独立交付。

### P0 — 收尾与加固(建议最先做)

- 轮换并在 git 中移除明文密钥,确认 `.env` 被忽略;提供 `.env.example`。
- 给 `/api/trip/plan` 加鉴权 + 速率限制 + 请求体大小/超时保护。
- 把 `print` 换成 `loguru`(已在依赖里)结构化日志,补 request id。
- 兜底行程显式标记降级(`success=False` 或 `degraded=True`),避免假数据当成功。
- 补最小测试集:JSON 工具、天气/POI 解析器、`/plan` 的 TestClient 冒烟(打桩 LLM)。

### P1 — 记忆与多轮对话

目标:支持"基于上一版行程做增量修改",而不是每次从零生成。

设计要点:

1. **短期记忆(会话内)**:给 `TripPlanningState` 增加 `session_id` / `messages`,
   用 LangGraph 的 **checkpointer** 持久化图状态:
   - 开发:`MemorySaver`(进程内)
   - 生产:`SqliteSaver` / `PostgresSaver`(可跨重启)
   调用改为 `graph.ainvoke(input, config={"configurable": {"thread_id": session_id}})`。
2. **长期记忆(跨会话)**:用 LangGraph 的 **Store**(`InMemoryStore` 或
   `PostgresStore`)保存用户偏好,例如"喜欢经济型酒店""不吃辣""带小孩"。
   在 `generate_plan` 前做一次 `store.search(...)` 把偏好注入提示词。
3. **接口改造**:`TripRequest` 增加可选 `session_id`(保持向后兼容,缺省时生成新会话);
   前端保存并回传。可新增 `POST /api/trip/plan/{session_id}/refine` 之类的"改行程"入口。
4. **人机协同(可选)**:需要"用户确认后再继续"时,用 LangGraph `interrupt` + 恢复。

### P2 — RAG 知识库

目标:让规划有"本地知识",而不只靠实时工具和模型记忆。

设计要点:

1. **数据源**:城市攻略、景点讲解、游记、开放时间/门票/最佳季节的整理文本,
   也可抓取维基/官方介绍(注意版权与来源标注)。
2. **索引**:LangChain `Document Loader` → `RecursiveCharacterTextSplitter`
   → Embedding(中文可用 BGE / `text-embedding` 系列)→ 向量库
   (本地 `FAISS`/`Chroma`,生产 `pgvector`/`Milvus`)。
3. **检索节点**:在 LangGraph 中新增一个与三路检索并行的 `retrieve_knowledge` 节点,
   或作为 `generate_plan` 的前置;把 top-k 片段拼进规划提示词。
4. **混合检索**:向量 + 关键词(BM25)混合,并对 `city` 做元数据过滤。
5. **引用与可信度**:返回片段时带来源,规划里可要求标注依据,降低幻觉。
6. **与实时工具分工**:RAG 负责"背景知识/攻略",MCP 负责"实时天气/POI/路线",
   两者在提示词中分区呈现。

### P3 — 性能与成本

- 把高德 MCP 从"每调用起进程"改为常驻连接(`streamable-http`),
  或直接用 `httpx` 封装高德 REST 成普通 Tool;对 POI/天气加 TTL 缓存。
- 结果级缓存:相同 `TripRequest` 命中缓存直接返回(注意 session/个性化时需 bypass)。
- `lifespan` 预热工具与图;给 fan-out 加 `Semaphore` 做并发背压。
- 评估更小/更快的模型做检索子任务,规划用强模型,做"模型分级"。

### P4 — 可观测性与评测

- 接入 LangSmith / Langfuse 追踪每次工具调用、token 消耗、耗时。
- 建立离线评测集(城市 × 偏好的 golden cases),度量:JSON 合法率、坐标一致性、
  预算完整率、三路检索成功率、端到端时延。
- 记录降级触发率(结构化输出失败率、兜底率),作为质量红线。

### P5 — 体验增强

- **流式输出**:用 SSE/WebSocket 把"景点检索完成 / 天气完成 / 规划中 / 完成"
  逐步推给前端,缓解 120s 空等。
- 前端可展示三路检索进度、让用户在中途修改偏好。
- 多轮改行程 UI(依赖 P1)。

---

## 六、记忆与 RAG 的落地位置建议

为方便后续动手,给出"改哪个文件、加什么"的落点映射:

| 能力 | 主要改动点 | 新增/变更 |
| --- | --- | --- |
| 会话记忆 | `agents/trip_planner_agent.py` | `_ensure_graph()` 里 `builder.compile(checkpointer=...)`;`plan_trip` 传 `thread_id` |
| 偏好长期记忆 | 新增 `services/memory_store.py` | LangGraph `Store`,在 `generate_plan` 前检索并注入 |
| 接口兼容 | `models/schemas.py`、`api/routes/trip.py` | `TripRequest` 加可选 `session_id`;新增 refine 路由 |
| RAG 索引 | 新增 `services/rag/`(loader/splitter/embedding/vectorstore) | 离线建库脚本 + 在线检索函数 |
| 检索节点 | `agents/trip_planner_agent.py` | `TripPlanningState` 加 `knowledge`;新增 `retrieve_knowledge` 节点接进图 |
| 可观测 | `agents`、`services`、`api` | 统一 logger;接入 LangSmith/Langfuse 回调 |
| 缓存/背压 | `services/amap_mcp.py`、`agents` | 工具级缓存;`Semaphore` 限流 |

---

## 七、一句话总结

**框架层面已经迁移干净:LangChain 工具 + LangGraph 并行编排 + 结构化输出,契约不变、
分层清晰。** 接下来真正决定产品体验的,是**记忆(多轮+偏好)、RAG(本地知识)、
性能(常驻工具+缓存+流式)、安全(密钥+鉴权)和可观测性**这五件事 ——
建议按 P0 → P1 → P2 → P3/P4 的顺序推进。