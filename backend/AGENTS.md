# AGENTS.md — 智能旅行规划助手(trip_planner)工作交接

> **每次开始工作前先读这个文件**;每次有实质进展后,更新第 4 节「进度日志」与第 5 节「下一步」。
> 最近更新:2026-10-16

---

## 1. 怎么跑、怎么验

| 事项 | 位置 / 命令 |
| --- | --- |
| 工作目录(可写根) | `E:\hello agents_demo\trip_planner\backend` |
| Python | `E:\hello agents_demo\.venv\Scripts\python.exe` |
| 跑测试(离线,不花额度) | `& 'E:\hello agents_demo\.venv\Scripts\python.exe' -X utf8 -m pytest -q` |
| 隔离诊断耗时(见 4.2,`tool`/`detail` 阶段不花额度) | `& 'E:\hello agents_demo\.venv\Scripts\python.exe' -X utf8 scripts\diag_p0_latency.py --stages tool,attraction,planner` |
| 一键启动前后端 | `powershell -ExecutionPolicy Bypass -File scripts\dev.ps1` |
| 端口 | 后端 8000 · 前端 5173 · 常驻高德 MCP Server 8001 |

**额度提醒**:一次 `POST /api/trip/plan` 会真实调用多次大模型。
只在必要时跑端到端,跑之前先跟用户说明。

**启动后端做验证的推荐姿势**(隐藏窗口 + 日志落盘 + 关热重载):

```powershell
$logDir = Join-Path $env:TEMP "trip_planner_8000"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$env:UVICORN_RELOAD='0'; $env:PYTHONUNBUFFERED='1'   # 不缓冲,便于实时看进度
Start-Process -FilePath "E:\hello agents_demo\.venv\Scripts\python.exe" `
  -ArgumentList @("-X","utf8","run.py") `
  -WorkingDirectory "E:\hello agents_demo\trip_planner\backend" `
  -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $logDir "out.log") `
  -RedirectStandardError (Join-Path $logDir "err.log") -PassThru
```

收尾务必清干净(后端 + 它拉起的 MCP Server 都会残留):

```powershell
netstat -ano | Select-String ':8000\s|:8001\s'      # 找出占用端口的 PID
Get-Process -Name python,uvx,uv -ErrorAction SilentlyContinue
```

---

## 2. 环境已知问题(别重复踩)

### 2.1 `apply_patch` 有故障(重要)

对**部分已存在文件**会误报
`Failed to read file to update ...: path contains a reparse point`。

- 实测**失败**:`app/services/amap_mcp.py`、`app/api/main.py`
- 实测**正常**:`.env.example`、`app/config.py`、`app/services/llm_service.py`、新建文件
- 文件系统层面完全正常:`fsutil reparsepoint query` 明确说不是重解析点,
  无 junction、无 ADS、属性普通。是工具自身的问题,不是路径问题。

绕过办法:

1. **小改动** → 提权用 PowerShell 做定点字符串替换。
   每次替换前先用 `.Contains()` 校验锚点存在,不存在就 `throw`;
   改完**必须 `git diff` 逐行核对**。
2. **整体重写** → 提权 `Remove-Item` 删除 → `apply_patch` `Add File`
   (新文件路径这条路是正常的)。

另外:写 `AGENTS.md` 这类含大量 `-` 开头的列表时,`apply_patch` 会要求每行都带 `+` 前缀,
漏一个就会被解析成 hunk 头而报 `not a valid hunk header`。

### 2.2 编码与换行

- 命令行传中文没问题(已验证 UTF-8 字节一致),但 PowerShell 5.1 的
  `Set-Content -Encoding UTF8` **会写 BOM**。写文件统一用:
  `[IO.File]::WriteAllText($p, $t, (New-Object Text.UTF8Encoding($false)))`
- 读文件统一 `Get-Content -Raw -Encoding UTF8`;
  控制台中文乱码时先 `[Console]::OutputEncoding=[System.Text.Encoding]::UTF8`。
- 仓库开着 `core.autocrlf`,写入用 LF 即可,`git diff` 只显示真实改动。

### 2.3 沙箱

- 只能写 `backend/`。改 `frontend/`、`README.md`、`.env` 需要提权。
- 出网(调 ModelScope / 高德)需要提权。
- `Get-CimInstance Win32_Process` 会被拒绝访问,查进程用 `Get-Process` / `netstat -ano`。

---

## 3. 架构与关键文件

一次 `/api/trip/plan` 的链路:

```
POST /api/trip/plan
  └─ agents/trip_planner_agent.py  MultiAgentTripPlanner.plan_trip_with_status()
       └─ LangGraph StateGraph
            ├─ search_attractions ─┐
            ├─ query_weather      ─┼─ 三路并行
            ├─ search_hotels      ─┘
            │     ↑ 三路都是 services/trip_retrieval.py 的**确定性调用**
            │       (直接调高德 MCP + 服务层解析,**0 次模型**)
            └─ generate_plan       → **唯一的模型调用**:response_format=TripPlan 结构化输出
                 失败 → planner_fallback + extract_json
      任何一步失败 → _degrade() 返回占位行程,degraded=true(不假装成功)
```

| 关注点 | 文件 |
| --- | --- |
| LangGraph 编排 / 规划 Agent / 兜底 | `app/agents/trip_planner_agent.py` |
| 三路检索(确定性,不经过模型) | `app/services/trip_retrieval.py` |
| 高德 MCP 连接与常驻 Server 生命周期 | `app/services/amap_mcp.py` |
| LLM 客户端(含 thinking 开关) | `app/services/llm_service.py` |
| 配置与凭据解析 | `app/config.py` |
| 高德结果解析 | `app/services/amap_service.py` |
| 对外契约(请求/响应模型) | `app/models/schemas.py` |
| 应用生命周期 / 路由注册 | `app/api/main.py` |

---

## 4. 进度日志

### 2026-10-16(第三轮:检索不再经过模型,单次请求 112s → 57s)

**第二轮指向的瓶颈是"模型调用次数",本轮把大头砍掉了:三路检索本来就是确定性的,
根本不需要 Agent。**

| | 改造前 | 改造后 |
| --- | --- | --- |
| 模型调用次数 | 10+ 次 | **1 次**(只有规划那一步) |
| 三路检索 | 3 个 LLM Agent(景点那一路实测 4 次调用 / 58.9s) | 确定性调用,**0 次模型** / 17.7s |
| 坐标 | 模型"看心情"发了 20 次 `maps_search_detail` | 服务层固定回填 6+6 个候选 |
| 端到端 | 63.9s / 112.1s | **57.5s**(`degraded=false`) |

1. **新增 `app/services/trip_retrieval.py`**:三路检索直接调高德 MCP 工具,再用
   `AmapService` 解析,不经过模型。
   - 景点:按前两个偏好词搜 POI → 按 id 去重 → **风景名胜(typecode ``11xxxx``)
     稳定排序到前面**(否则用"美食"搜索会把饭馆混进景点清单)→ 回填详情。
   - 天气:`maps_weather` → `_parse_weather` → 直接渲染成规划提示词要的字段顺序。
   - 酒店:偏好词 + "酒店"两个关键词搜索 → 回填详情(坐标 / 评分)。
2. **坐标回填一并解决(P1 完成)**:坐标取自 `maps_search_detail`,实测是真值
   (八达岭 `116.016802,40.356188`、故宫 `116.397029,39.917839`),不再是模型编的。
3. **回填数量有上界**:`_detail_budget(days) = min(12, max(6, days*3))`。
   detail 在高德 MCP 上是**串行排队**的(第二轮实测),所以这个数直接等于耗时。
4. **`app/agents/trip_planner_agent.py`**:删掉三个检索 Agent 及其三段提示词,
   节点简化成 `return {"attraction_result": await retrieve_attractions(...)}` 这种一行。
   整个文件现在只剩一个模型用途:规划 + 结构化输出。
5. **诊断脚本同步**:新增 `retrieval` 阶段(`tool`/`detail`/`retrieval` 全部零额度),
   `graph` 阶段现在也只要 1~2 次模型调用。

测试:85 全绿(新增 `tests/test_trip_retrieval.py`)。

### 2026-10-16(第二轮:端到端终于跑通 `degraded=false`)

**结论先行:上一轮把瓶颈归到"MCP 已修好、剩下是模型侧延迟"并不准确。**
真实原因是**规划 Agent 的结构化输出一直在校验失败,模型把同一份参数原样重发,
一路重试到 recursion_limit** —— 看起来像"卡住",其实是在死循环烧额度。

用 `scripts/diag_p0_latency.py` 把链路拆开计时(北京 2 日游)得到的实测数字:

| 阶段 | 墙钟 | 说明 |
| --- | --- | --- |
| `maps_text_search` 单次 | **1.2s** | 与上一轮结论一致 |
| `maps_search_detail` 串行 | **0.9s/次** | |
| `maps_search_detail` 10 并发 | **7.6~9.3s/次,整批 9.3s** | 服务端排队,并发**没有任何收益** |
| 景点检索 Agent | **58.9s** | 4 次 LLM(40.6s)+ 20 次工具调用 |
| 规划 Agent(**修复前**) | 100.6s 仍失败 | 6 次调用全是 `finish=tool_calls` |
| 规划 Agent(**修复后**) | **17.6s 一次成功** | `structured=True`,2 天 3 景点 |
| 真实全链路(修复后) | **63.9s / 112.1s** | 两次都是 `degraded=false` |

1. **修掉规划 Agent 的死循环(本轮核心)**
   - 现象:6 次 LLM 调用(100s)全是 `finish=tool_calls`,请求工具都是 `TripPlan`,
     每次参数**长得一模一样**,最后仍然失败。
   - 根因(从消息序列里抓到的原话):
     `Failed to parse data to TripPlan: 1 validation error for TripPlan budget`
     `Input should be a valid dictionary or instance of Budget`
     `[input_value='{"total_attractions": 10...', input_type=str]`
     —— 模型把 `budget` 当成 **JSON 字符串**返回,而不是对象。LangChain 校验失败后
     回灌错误让模型修正,模型却把同样的参数再发一遍,于是进入循环。
   - 修法(`app/models/schemas.py`):新增 `_coerce_json_container()` /
     `_coerce_location()`,在 `mode="before"` 阶段把"长得像 JSON 的字符串"还原成
     对象/数组;坐标额外兼容 `"lng,lat"` 字符串。
     **刻意不改提示词**:让 schema 能吃下畸形输出,比求模型每次都输出对更可靠。
   - 验证:同一个规划 Agent 从"100.6s 仍失败"变成 **17.6s 一次成功**。
2. **给 Agent 加收敛边界**(`app/config.py` + `app/agents/trip_planner_agent.py`)
   - `AGENT_RECURSION_LIMIT`(默认 12):单个检索/规划 Agent 的循环上限。
     上一轮实测"6 次重试 = 100 秒",不设边界就是 11 分钟。
   - `PLAN_TOTAL_TIMEOUT`(默认 240s):整条 `/plan` 的墙钟总闸。
     `LLM_TIMEOUT` 只管"两次读取之间的间隔",对流式响应不构成总时长约束 ——
     这一条上一轮就写进了待办,现在补上。
   - `LLM_MAX_TOKENS`(默认 0 = 不限):开关留着但默认关。实测失败原因是 schema
     不匹配、不是输出过长;而截断会让结构化输出直接判失败,所以默认不加约束。
3. **修掉子 Agent 的静默失败**(同文件)
   - 现象:第一次真跑全链路时酒店检索静默失败,日志里只有
     `unhandled errors in a TaskGroup (1 sub-exception)` —— 和上一轮 MCP 崩溃
     一模一样的"真因被埋"。(查过服务端日志:没有 500,只有 Windows 上
     客户端连接被重置的痕迹。)
   - 修法:`_run_agent` 改用 `root_cause()` 打印真因,并对瞬时失败**重试一次**。
     实测:重跑后三个检索 Agent 全部成功,不再有静默失败。
4. **新增隔离诊断脚本** `scripts/diag_p0_latency.py`(用法见第 1 节)
   - `--stages tool,detail` 完全不调用大模型,零额度,可以放心跑。
   - `--dump-messages` 打印完整消息序列 —— 这次就是靠它抓到 `budget` 的原始报错。
   - `--recursion-limit` 可调小,便于快速复现"重试循环"。

### 2026-10-16

**最大的教训:50 个离线测试全绿 + 四份文档齐全,但真实 `/plan` 从一开始就是坏的。**
判断"这项目能不能跑"必须跑真实端到端,不能只看测试和文档。

1. **修掉 MCP 启动崩溃**(`app/services/amap_mcp.py`)
   - 现象:`/plan` 请求 200 秒后返回占位行程,
     日志里只有 `unhandled errors in a TaskGroup (1 sub-exception)`。
   - 根因:`uvx` 每次都**现场解析**依赖,解出 pydantic **2.14.1**;
     而 `amap-mcp-server 0.1.11` 硬锁 `mcp==1.8.1`,mcp 1.8.1 会
     `from pydantic._internal._typing_extra import eval_type_backport` ——
     该符号在 pydantic 2.14 中被删除。子进程 import 阶段直接崩,
     客户端只看到 `Connection closed`。
   - 这是**依赖腐化**,不是谁改了代码:2026-09-28 重构时还能加载 16 个工具,
     之后 pydantic 发新版就自己坏了。
   - 修法:uvx 启动参数加 `--with "pydantic<2.14"`。**已验证**:16 个工具加载成功。
2. **修掉静默失败**(同文件):新增 `root_cause()`,把 ExceptionGroup 剥到叶子节点,
   失败原因不再被埋掉。
3. **修掉结构化输出 400**(`llm_service.py` + `config.py`)
   - 现象:`400 InvalidParameter: The tool_choice parameter does not support
     being set to required or object in thinking mode`。
   - 根因:`response_format=TripPlan` 被 LangChain 翻译成「强制工具调用」,
     而 Qwen3.5 在 **thinking 模式**下不接受 `tool_choice=required`。
   - 修法:新增 `LLM_ENABLE_THINKING`(默认 `false`),
     给 `ChatOpenAI` 传 `extra_body={"enable_thinking": ...}`。
   - **已验证**:关掉 thinking 后 `create_agent(response_format=TripPlan)`
     能返回真正的 `TripPlan`。另外实验过 `with_structured_output(method="json_mode")`,
     模型返回的结构不合规,**比现在更差,别用**。
4. **传输层 stdio → 常驻 streamable-http**(`amap_mcp.py`,用户选定的「方案 A」)
   - 原因:`MultiServerMCPClient` 的 stdio 模式**每次工具调用都新建会话
     = 重新拉起一个 uvx 进程**(实测单次数秒)。一次 `/plan` 有 20+ 次工具调用,
     直接把请求从秒级推到分钟级(实测客户端 900 秒超时打满仍未返回)。
   - 改法:惰性拉起**一个常驻** `amap-mcp-server streamable-http`
     (默认 `127.0.0.1:8001`),通过 HTTP 连接;
     退出时用 `taskkill /F /T` 整棵树回收
     (uvx 会派生 python 子进程,只 kill uvx 会留下孤儿占着端口);
     `AMAP_MCP_URL` 可表示「由外部托管,别插手它的启停」。
   - **已验证**:服务器启动 +6.5s、16 工具加载 1.45s、
     **单次工具调用 ~1.3s**(其中绝大部分是 `restapi.amap.com` 本身的延迟);
     应用内的「惰性启动」与「复用已监听实例」两条路径都跑通。
5. **坐实了坐标问题(还没修)**:`maps_text_search` 返回的 POI **不含坐标**
   (实测 20 条 `location` 全空,只有 id/name/address/typecode),
   而规划提示词第 9 条却写着「景点经纬度必须真实准确」——模型手里没有坐标,只能编。
   `maps_search_detail(poi_id)` 能拿到真坐标(`116.397029,39.917839`)
   以及 `rating=4.9`、`level=AAAAA`、`opentime`。
   **修复路径已验证可行,代码未动。**
6. **外部依赖**:ModelScope 账号原先未做邮箱验证(所有模型调用 403),用户已解决;
   免费档还有 429 限流,而一次 `/plan` 要打多次模型,两者冲突。

测试:50 → **61 全绿**(新增 `tests/test_amap_mcp.py`)。
本轮改动已提交为 `6452c1b`(见第 6 节)。

### 仍未解决(截至第三轮)

- ✅ **端到端稳定跑出 `degraded=false`**(第三轮 57.5s;第二轮 63.9s / 112.1s)。
- ✅ **坐标已真实回填**(P1 完成,见第 5 节)。
- ⚠️ **剩下的 57 秒里,那唯一一次模型调用占 25~30 秒**。
  调用次数已经压到 1 次,再想提速只能靠结果缓存 / 流式输出 / 换更快的模型,
  不属于"编排问题"了。
- ⚠️ **`maps_search_detail` 串行排队**:10 并发时单次从 0.9s 涨到 7.6~9.3s,
  总时间和串行一样。所以检索层刻意**串行**调用,靠控制条数(6+6)而不是靠并发。
- ⚠️ **`langchain-mcp-adapters 0.3.2` 每次工具调用都会新建 MCP 会话**
  (日志里是 session create + ListToolsRequest + CallToolRequest + DELETE 一整套)。
  调用次数从 20+ 降到 12 之后连接重置的几率小了很多,但根因还在。
- ⚠️ **一次 `/plan` 的检索是 12 次 detail 串行**,约 11 秒;
  如果哪天允许"不查详情的景点",这块还能再省。

---

## 5. 下一步

### P0 · 从 57 秒继续往下压(剩下的全在那"一次模型调用"上)

三路检索已经变成确定性调用(0 次模型),整条链路**只剩 1 次模型调用**,
而这一次就要 25~30 秒 —— 这是 ModelScope 单次生成的固有延迟,不是编排问题。
可行方向:

1. **结果级缓存**:同一 (city, preferences, 日期, 住宿) 直接返回上次的 `TripPlan`(TTL)。
   这是唯一能把"重复请求"压到秒级的手段。
2. **流式输出**:规划那一步边生成边推给前端(`/plan` 改 SSE),
   用户不用干等 60 秒白屏,前端超时压力也随之消失。
3. **缩小输出体积**:`TripPlan` 结构偏大(每天酒店 + 三餐 + 2~3 景点 + 预算)。
   若接受"预算 / 餐食由服务层兜底",模型输出能明显变短,生成时间随之下降。
4. **换更快的模型**:同一份提示词换个更小更快的模型量一次,就知道差距有多大。

> 诊断工具已经就位:`--stages tool,detail,retrieval` **完全不花额度**;
> `--stages graph` 现在也只要 1~2 次模型调用。

### P1 · 坐标回填 —— ✅ 已完成(第三轮)

- `maps_text_search` 返回的 POI **不含坐标**(复测 20 条 `location` 全空),
  `maps_search_detail(poi_id)` 能拿到真坐标。
- 现在由 `app/services/trip_retrieval.py` 在**服务层固定回填**,
  不再依赖"模型记得去查";顺带把 `rating` / `opentime` / 门票也带了进来。

### P2 · 工程化

> 原本有一份 `NEXT_STEPS_CHECKLIST.md` 和一份 `BACKEND_REVIEW_AND_OPTIMIZATION.md`
> 写着更细的路线图,按用户要求已删除(历史版本仍能从 git 里翻出来)。
> 其中还值得做、且别处没记的部分收在这里:

- **结构化日志**:现在还是 `print` + emoji,`loguru` 已在依赖里;补 request id 与耗时。
- **真实依赖健康检查**:现在只查 key 存不存在,应实际 ping 一次 MCP / LLM。
- **限流 + 并发背压**:一个 `/plan` 会 fan-out 出 3 个检索 Agent,每个又多次调工具。
- **成本护栏**:按调用量 / token 统计,设单用户日配额(直接关系到额度被刷爆)。
- **结果级缓存**:相同 `TripRequest` 命中缓存直返(TTL);POI / 天气也可做服务级缓存。
- **接口鉴权与配额**:`/api/trip/plan` 目前无鉴权,任何人都能拿来刷你的额度。
- **可观测性**:接入 LangSmith / Langfuse,追踪工具调用、token、耗时。
- **评测集**:城市 × 偏好的 golden cases,度量 JSON 合法率、端到端时延、兜底率。
- **Docker 化**:注意镜像里得装上 `uvx`(高德 MCP 靠它拉起)。
- **密钥轮换**:`.env` 从未进过版本库,但 ModelScope / 高德 / Unsplash 的 key
  建议手动轮换一次 —— 这条原本只写在被删掉的那两份文档里,别跟着一起丢了。
- **清理配置过渡残留**:`config.py` 仍加载 `HelloAgents/.env`、保留 `openai_*` 字段。

### 明确不要做

**在单轮请求都还没跑通之前,不要动记忆 / RAG。**

---

## 6. 本轮改动清单(已提交 `6452c1b`)

```
 M README.md                            # 端口说明 + 已知限制(原「每次调用起进程」已修)
 M backend/.env.example                 # MCP 常驻配置 + LLM_ENABLE_THINKING 说明
 M backend/app/api/main.py              # 关闭时回收常驻 MCP Server
 M backend/app/config.py                # 新增 llm_enable_thinking
 M backend/app/services/amap_mcp.py     # pydantic 约束 + root_cause + streamable-http 常驻
 M backend/app/services/llm_service.py  # extra_body enable_thinking
?? backend/tests/test_amap_mcp.py       # 新增用例覆盖上述改动
```

改动都用 `git diff` 逐行核对过,提交前测试全绿。

> 提示:提交后如果又改了代码,记得在**提交前**回来更新本节,别让这里和仓库状态对不上。

### 第二轮(端到端跑通,已提交 `16a3d3f` / `ad7c18d`)

```
 M backend/AGENTS.md                        # 本节 + 第 4/5 节
 M backend/.env.example                     # 新增 LLM_MAX_TOKENS / AGENT_RECURSION_LIMIT / PLAN_TOTAL_TIMEOUT 说明
 M backend/app/agents/trip_planner_agent.py # recursion_limit + 总时长闸门 + root_cause 报错 + 瞬时失败重试
 M backend/app/config.py                    # 新增 agent_recursion_limit / plan_total_timeout / llm_max_tokens
 M backend/app/models/schemas.py            # 嵌套结构 JSON 字符串容错(本轮核心修复)
 M backend/app/services/llm_service.py      # 可选的 max_tokens
 M backend/tests/test_degradation.py        # 假图签名适配 + 总时长超时降级用例
?? backend/scripts/diag_p0_latency.py       # 新增:隔离诊断脚本
?? backend/tests/test_schema_tolerance.py   # 新增:结构化输出容错用例
```

改动同样用 `git diff` 逐行核对过,`pytest` **72 全绿**(上一轮 61 + 新增 11),
并且真实跑通了两次全链路(`degraded=false`)。

### 第三轮(检索去模型化,尚未提交)

```
 M backend/AGENTS.md                          # 第 3/4/5/6 节
 M README.md                                  # 架构描述与已知限制同步
 M backend/app/agents/trip_planner_agent.py   # 删掉三个检索 Agent 及其提示词
?? backend/app/services/trip_retrieval.py     # 新增:确定性三路检索
 M backend/scripts/diag_p0_latency.py         # 新增 retrieval 阶段,替换已删除的检索 Agent 阶段
?? backend/tests/test_trip_retrieval.py       # 新增:检索层离线用例
```

`pytest` **85 全绿**;真实全链路 57.5s / `degraded=false`(改造前 63.9~112.1s)。
