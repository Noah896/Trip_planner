# trip_planner 从 HelloAgents 重构为 LangChain 框架

> 重构完成时间:2026-09-28
> 重构范围:`trip_planner/backend`(FastAPI 后端)
> 前端 `trip_planner/frontend` **未做任何改动**

---

## 一、重构目标

把后端对 `hello-agents` 框架的依赖整体替换为 LangChain 生态,同时**保持对外接口契约不变**:

- `POST /api/trip/plan` 的请求体(`TripRequest`)与响应体(`TripPlanResponse`)结构不变
- `GET /health`、`GET /api/trip/health`、`GET /api/map/health` 等健康检查语义不变
- 前端 `frontend/src/services/api.ts` 无需任何修改

核心就是把三样东西换掉:**LLM 客户端**、**工具封装**、**Agent 编排**。

---

## 二、依赖变更

### 移除

| 依赖 | 原因 |
| --- | --- |
| `hello-agents[protocols]>=0.2.4,<=0.2.9` | 被 LangChain 体系整体替代,代码中已无任何引用 |
| `fastmcp>=2.0.0` | **与 `langchain-mcp-adapters` 冲突**:后者要求 `mcp>=1.24`,而 fastmcp 2.12.5 要求 `mcp<1.17.0`。项目实际不使用 fastmcp |

### 新增

| 依赖 | 本次实际安装版本 |
| --- | --- |
| `langchain>=1.0` | 1.4.2 |
| `langchain-openai>=1.0` | 1.6.6 |
| `langgraph>=1.0` | 1.2.12 |
| `langchain-mcp-adapters>=0.3` | 0.3.2 |
| `mcp>=1.24.0` | 1.30.0 |

> `langchain-core` 1.6.5 作为传递依赖安装。
> 环境中原有的 `langchain` / `langchain-openai` / `langgraph` 已存在,本次只需补装 `langchain-mcp-adapters`(它会顺带把 `mcp` 从 1.16.0 升到 1.30.0)。

---

## 三、架构对比

### 重构前(HelloAgents)

```
POST /api/trip/plan
   └── MultiAgentTripPlanner.plan_trip()   ← 同步阻塞
         ├── attraction_agent.run()  ─┐
         ├── weather_agent.run()      ├─ 4 次串行 LLM 调用
         ├── hotel_agent.run()       ─┘
         └── planner_agent.run()      → 文本里手工抠 JSON
                  ↑
           共享 1 个 MCPTool(amap),靠 "[TOOL_CALL:...]" 文本协议调度
```

### 重构后(LangChain + LangGraph)

```
POST /api/trip/plan
   └── MultiAgentTripPlanner.plan_trip()   ← async
         └── LangGraph StateGraph.ainvoke()
               ├── search_attractions ─┐
               ├── query_weather       ├─ 三路并行 fan-out
               ├── search_hotels      ─┘
               └── generate_plan      → response_format=TripPlan 结构化输出
                        ↑
              create_agent + 原生 function calling
              MultiServerMCPClient → uvx amap-mcp-server(16 个工具)
```

关键收益:

1. **检索阶段并行**。三个检索节点互不依赖,由 LangGraph 同时调度,而不是像原来那样串行阻塞。由于高德 MCP 的**每次工具调用都会新起一个 `uvx` 子进程**(实测每次数秒),并行带来的收益很明显。
2. **原生 function calling**。删除了提示词里整段 `[TOOL_CALL:xxx:key=value]` 格式说明和配套的正则解析逻辑。
3. **结构化输出**。不再需要用 `find("```json")` 手工切字符串,`TripPlan` 由 Pydantic schema 直接约束(并保留 JSON 解析兜底)。

---

## 四、核心概念映射

| HelloAgents | LangChain 对应物 | 说明 |
| --- | --- | --- |
| `HelloAgentsLLM` | `langchain_openai.ChatOpenAI` | 同为 OpenAI 兼容协议;差别是后者需显式传 `model` / `base_url` / `api_key` |
| `hello_agents.tools.MCPTool` | `langchain_mcp_adapters.MultiServerMCPClient` | 都走 MCP 协议;工具名从 `amap_maps_text_search` 变为 `maps_text_search` |
| `SimpleAgent` | `langchain.agents.create_agent` | 原生 function calling 取代 `[TOOL_CALL:...]` 文本协议 |
| `SimpleAgent.add_tool()` | `create_agent(tools=[...])` | 工具在构造时注入,而非事后追加 |
| 顺序调用 4 个 `run()` | LangGraph `StateGraph` | 三路检索并行 fan-out + 汇聚 |
| `_parse_response()` 抠 JSON | `response_format=TripPlan` | 结构化输出,保留 JSON 兜底 |
| `@app.on_event("startup")` | FastAPI `lifespan` | 原 API 在新版 FastAPI 已弃用 |

---

## 五、逐文件改动清单

### 新增

| 文件 | 说明 |
| --- | --- |
| `backend/app/utils/__init__.py` | 新建工具包 |
| `backend/app/utils/json_utils.py` | `extract_json()` / `parse_mcp_payload()` / `json_dumps()`:从 LLM、MCP 返回文本中稳健抽取 JSON |
| `backend/app/services/amap_mcp.py` | 高德 MCP ↔ LangChain Tool 桥接层:构建 stdio 连接、加载并缓存工具清单、按名查找工具、归一化工具返回值 |

### 重写

| 文件 | 改动要点 |
| --- | --- |
| `backend/app/services/llm_service.py` | `HelloAgentsLLM()` → `ChatOpenAI(...)`,凭据由 `config.resolve_llm_credentials()` 解析 |
| `backend/app/services/amap_service.py` | 移除 `MCPTool`;全部方法改为 **async**;补全原先标 `TODO` 的 POI / 天气 / 路线 / 地理编码结果解析 |
| `backend/app/agents/trip_planner_agent.py` | 核心重构:4 个 `SimpleAgent` → 4 个 `create_agent`;串行调用 → LangGraph `StateGraph`;提示词去掉 `[TOOL_CALL:...]` 协议段;新增 `describe()`、惰性建图 |
| `backend/app/api/routes/trip.py` | `/plan` 改为 `await agent.plan_trip(...)`;`/health` 改用 `agent.describe()`(原实现引用了已不存在的 `agent.agent.name` / `list_tools()`) |
| `backend/app/api/main.py` | `@app.on_event` → `lifespan` 上下文管理器;API 描述从"基于HelloAgents框架"改为"基于 LangChain + LangGraph" |

### 修改

| 文件 | 改动要点 |
| --- | --- |
| `backend/app/api/routes/map.py` | 三处服务调用加 `await`;`/health` 不再访问已删除的 `mcp_tool._available_tools`,改为检查 API Key 配置 |
| `backend/app/api/routes/poi.py` | 服务调用加 `await`;Unsplash 的阻塞 `requests` 调用改用 `asyncio.to_thread()` 避免阻塞事件循环 |
| `backend/app/config.py` | 新增 `llm_*` / `modelscope_api_key` 配置字段与 `resolve_llm_credentials()`;`validate_config()` / `print_config()` 改用新解析逻辑;`app_name` 由"HelloAgents智能旅行助手"改为"智能旅行规划助手" |
| `backend/app/models/schemas.py` | `POIInfo.location` 由必填改为可选(高德文本搜索接口不返回经纬度,详见"踩坑记录") |
| `backend/app/__init__.py` | 包描述更新 |
| `backend/requirements.txt` | 见"依赖变更" |

### 未改动

| 文件 | 说明 |
| --- | --- |
| `trip_planner/frontend/**` | 接口契约未变,零改动 |
| `backend/run.py` | 启动方式不变(`uvicorn` 直接加载 `app.api.main:app`) |
| `backend/.env` | 变量名保持兼容(`LLM_MODEL_ID` / `LLM_BASE_URL` / `MODELSCOPE_API_KEY` / `AMAP_API_KEY`) |
| `backend/app/models/schemas.py` 中的 `TripRequest` / `TripPlan` / `TripPlanResponse` | 对外契约,刻意保持不变 |

---

## 六、关键代码对照

### 1. LLM 客户端

```python
# 重构前
from hello_agents import HelloAgentsLLM
_llm_instance = HelloAgentsLLM()   # 自动推断 provider

# 重构后
from langchain_openai import ChatOpenAI
model, api_key, base_url = settings.resolve_llm_credentials()
_llm_instance = ChatOpenAI(
    model=model, api_key=api_key, base_url=base_url or None,
    temperature=settings.llm_temperature, timeout=settings.llm_timeout,
)
```

`HelloAgentsLLM` 会自动探测 provider 并从 `MODELSCOPE_API_KEY` / `LLM_BASE_URL` 兜底,
`ChatOpenAI` 不做这件事 —— 因此新增了 `Settings.resolve_llm_credentials()`
按 `llm_api_key → modelscope_api_key → 环境变量` 的优先级补齐这层兼容。

### 2. MCP 工具

```python
# 重构前
from hello_agents.tools import MCPTool
self.amap_tool = MCPTool(
    name="amap", description="高德地图服务",
    server_command=["uvx", "amap-mcp-server"],
    env={"AMAP_MAPS_API_KEY": settings.amap_api_key},
    auto_expand=True,
)
agent.add_tool(self.amap_tool)   # 展开成 amap_maps_text_search 等

# 重构后
from langchain_mcp_adapters.client import MultiServerMCPClient
client = MultiServerMCPClient({
    "amap": {
        "transport": "stdio",
        "command": "uvx",
        "args": ["amap-mcp-server"],
        "env": {"AMAP_MAPS_API_KEY": settings.amap_api_key},
    }
})
tools = await client.get_tools(server_name="amap")   # tools 直接传给 create_agent
```

`find_tool()` 同时兼容 `maps_text_search` 与带前缀的 `amap_maps_text_search`,
所以旧命名习惯的代码也能平滑迁移。

### 3. Agent 编排

```python
# 重构前:4 个 SimpleAgent 串行
attraction_response = self.attraction_agent.run(attraction_query)
weather_response = self.weather_agent.run(weather_query)
hotel_response = self.hotel_agent.run(hotel_query)
planner_response = self.planner_agent.run(planner_query)
trip_plan = self._parse_response(planner_response, request)

# 重构后:LangGraph 三路并行 + 汇聚
builder = StateGraph(TripPlanningState)
for node in ("search_attractions", "query_weather", "search_hotels"):
    builder.add_edge(START, node)
    builder.add_edge(node, "generate_plan")
builder.add_edge("generate_plan", END)
final_state = await graph.ainvoke({"request": request})
plan = final_state["plan"]        # 已是 TripPlan 对象
```

### 4. 提示词

重构前的 `ATTRACTION_AGENT_PROMPT` 里有大段:

```
**工具调用格式:**
`[TOOL_CALL:amap_maps_text_search:keywords=景点关键词,city=城市名]`
...
2. 格式必须完全正确,包括方括号和冒号
```

重构后**整段删除**。LangChain 通过原生 function calling 调度工具,
模型由工具 schema 得知参数,无需在提示词里教它写协议。

---

## 七、踩坑记录

### 1. `fastmcp` 与 `langchain-mcp-adapters` 的依赖冲突

安装 `langchain-mcp-adapters` 会把 `mcp` 从 1.16.0 升到 1.30.0,
而环境里的 `fastmcp 2.12.5` 要求 `mcp<1.17.0`,pip 会报依赖冲突。

处理:项目本身不使用 fastmcp(它只是 hello-agents 的传递依赖),
因此从 `requirements.txt` 中移除。**注意**:如果你其它章节的代码
(`chapter*/`)依赖 fastmcp,请为它们单独建虚拟环境。

### 2. 高德 MCP 的返回结构和预期不一致

初版解析器按高德开放平台文档写的,实测全部返回空。抓取真实返回后发现:

**`maps_text_search` 不返回经纬度,也不返回电话:**

```json
{"suggestion": {"keywords": [], "cities": []},
 "pois": [{"id": "B000A8UIN8", "name": "故宫博物院",
           "address": "景山前街4号", "typecode": "110201|140100"}]}
```

→ 处理:`POIInfo.location` 改为可选;`type` 兼容 `typecode` 字段。
需要坐标时得再调 `maps_search_detail`。

**`maps_weather` 的 `forecasts[]` 下直接就是每日天气,没有 `casts` 这一层:**

```json
{"city": "北京市",
 "forecasts": [{"date": "2026-09-28", "dayweather": "多云",
                "nightweather": "小雨", "daytemp": "24", "nighttemp": "17",
                "daywind": "南", "daypower": "1-3"}]}
```

→ 处理:`_parse_weather()` 同时兼容 `lives[]`、`forecasts[]` 平铺、
`forecasts[].casts[]` 嵌套三种结构。

**`maps_search_detail` 才带坐标(字符串 `"经度,纬度"`)与评分:**

```json
{"id": "B000A8UIN8", "name": "故宫博物院", "location": "116.397029,39.917839",
 "address": "景山前街4号", "type": "风景名胜;...;博物馆", "rating": "4.9", "level": "AAAAA"}
```

### 3. MCP 每次调用都会新起进程

`MultiServerMCPClient` 的设计是"每次工具调用创建一个新 session",
即每次工具调用都会重新拉起 `uvx amap-mcp-server`(实测单次数秒)。
这也是把三路检索改成并行的主要原因 —— 否则 3 次串行检索会白白多花很多时间。

如果后续想进一步提速,可以考虑改用常驻的 streamable-http 传输,
或把高德 REST API 直接封装成普通 LangChain Tool(不走 MCP)。

### 4. 结构化输出未必被模型支持

ModelScope 上的 `Qwen/Qwen3.5-27B` 对 `response_format=TripPlan`
(本质是 `ToolStrategy`)的支持取决于其 function calling 实现是否完备。
因此实现了**双路径**:

1. 优先走 `planner_agent`(`response_format=TripPlan`),从
   `result["structured_response"]` 取 `TripPlan`;
2. 一旦抛错或拿不到结构,自动退回 `_planner_fallback`(纯文本 Agent)
   + `extract_json()` + Pydantic 校验。

### 5. 旧健康检查引用了已不存在的 API

原 `GET /api/trip/health` 用了 `agent.agent.name` 和 `agent.agent.list_tools()`,
这是 HelloAgents `SimpleAgent` 的接口,重构后必然 500。
新实现改用 `MultiAgentTripPlanner.describe()`,并且**不会触发工具加载**
(工具与 LangGraph 在首次 `/plan` 调用时才惰性构建,避免启动时拉起 uvx 子进程)。

---

## 八、验证记录

### 1. 静态验证

- `python -m compileall app` → 通过(exit 0)
- 全量 `rg` 检索确认 `app/` 下已无 `hello_agents` / `MCPTool` / `SimpleAgent` 的实际引用
  (仅剩注释中说明"从 HelloAgents 迁移"的文字)

### 2. LangGraph 构建与解析(离线)

```
graph type: CompiledStateGraph
nodes: ['__end__', '__start__', 'generate_plan', 'query_weather',
        'search_attractions', 'search_hotels']
describe: {'agent_name': '多智能体旅行规划系统(LangChain)',
           'framework': 'langchain+langgraph',
           'model': 'Qwen/Qwen3.5-27B',
           'graph_ready': True, 'tools_loaded': 0}
兜底方案: 3 天行程 / 每天 2 景点 + 早中晚三餐
JSON 兜底解析: 正确解析出 city=北京 / suggestion=注意防晒
```

### 3. FastAPI 路由(TestClient)

| 请求 | 状态 | 响应要点 |
| --- | --- | --- |
| `GET /` | 200 | `framework: langchain+langgraph` |
| `GET /health` | 200 | healthy |
| `GET /api/trip/health` | 200 | `graph_ready`, `model: Qwen/Qwen3.5-27B` |
| `GET /api/map/health` | 200 | `framework: langchain-mcp-adapters` |

### 4. 高德 MCP 实测(真实网络调用)

```
✅ 已加载高德 MCP 工具 16 个:
   maps_regeocode, maps_geo, maps_ip_location, maps_weather,
   maps_bicycling_by_address, maps_bicycling_by_coordinates,
   maps_direction_walking_by_address, maps_direction_walking_by_coordinates,
   maps_direction_driving_by_address, maps_direction_driving_by_coordinates,
   maps_direction_transit_integrated_by_address,
   maps_direction_transit_integrated_by_coordinates,
   maps_distance, maps_text_search, maps_around_search, maps_search_detail
```

修正解析器后的离线回放验证(使用上述真实返回体):

```
POI count: 2    (故宫博物院 / 故宫博物院-午门,type 取自 typecode)
Weather count: 2 (2026-09-28 多云/小雨 day 24 night 17 南 1-3, ...)
Nested form count: 1   (兼容 forecasts[].casts[])
Lives form count: 1    (兼容 lives[])
Detail location: 116.397029 39.917839
```

> 说明:本次未跑完整的 `POST /api/trip/plan` 端到端流程(它会产生 4 次
> 大模型调用并消耗你的 API 额度)。建议你按下面方式自行验证一次。

---

## 九、如何运行

```powershell
# 1. 安装/更新依赖
cd "E:\hello agents_demo\trip_planner\backend"
E:\hello agents_demo\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 启动后端
E:\hello agents_demo\.venv\Scripts\python.exe run.py

# 3. 验证健康检查
curl http://localhost:8000/api/trip/health

# 4. 端到端验证
curl -X POST http://localhost:8000/api/trip/plan `
  -H "Content-Type: application/json" `
  -d '{\"city\":\"北京\",\"start_date\":\"2026-10-01\",\"end_date\":\"2026-10-03\",\"travel_days\":3,\"transportation\":\"公共交通\",\"accommodation\":\"经济型酒店\",\"preferences\":[\"历史文化\"]}'
```

前置条件:

- `.env` 中 `AMAP_API_KEY` 必填(缺失会导致启动时配置校验失败)
- `.env` 中 `MODELSCOPE_API_KEY` / `LLM_MODEL_ID` / `LLM_BASE_URL` 用于 LLM
- 首次 `/plan` 调用会通过 `uvx` 拉取 `amap-mcp-server`,需要网络

---

## 十、已知限制与后续建议

1. **首次请求较慢**。首个 `/plan` 会加载 MCP 工具并建立连接,耗时较长。
   如需要,可在 `lifespan` 里预热 `await get_amap_tools()`。
2. **每次工具调用新起进程**。参见"踩坑记录 3";若要优化,建议改用常驻
   streamable-http 传输,或直接用 `httpx` 封装高德 REST API 成普通 Tool。
3. **`/api/map/poi` 不返回坐标**。这是高德文本搜索接口本身的行为,
   不是重构引入的问题;需要坐标请调 `/api/poi/detail/{poi_id}`。
4. **未接入持久化/断点续跑**。LangGraph 支持 `checkpointer`,若后续需要
   中断恢复或人工审批环节(interrupt),可在此基础上扩展。
5. **`hello-agents` 包仍在虚拟环境中**。`requirements.txt` 已移除它,
   但未从 venv 卸载(避免影响 `chapter*/` 目录下的其它示例)。
   如需彻底清理:`pip uninstall hello-agents fastmcp`。

---

## 十一、回滚方式

本次重构未使用 git 提交,如需回滚:

1. 将 `backend/requirements.txt` 恢复为 `hello-agents[protocols]>=0.2.4,<=0.2.9` + `fastmcp>=2.0.0`
2. 恢复以下文件的旧实现(可从对话历史或备份中取回):
   - `app/agents/trip_planner_agent.py`
   - `app/services/llm_service.py`
   - `app/services/amap_service.py`
   - `app/api/main.py`
   - `app/api/routes/trip.py`
   - `app/config.py` / `app/models/schemas.py` 中对应的改动
3. 删除新增的 `app/utils/` 与 `app/services/amap_mcp.py`
4. `pip install -r requirements.txt` 后重启

> 建议后续把这次改动纳入 git 管理,便于 diff 与回滚。
