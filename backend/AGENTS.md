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
| 隔离诊断耗时(见第 4 节,`tool`/`detail`/`retrieval` 都不花额度) | `& 'E:\hello agents_demo\.venv\Scripts\python.exe' -X utf8 scripts\diag_p0_latency.py --stages tool,retrieval,planner` |
| 验证流式端点(**会花 1 次模型额度**) | 先起后端,再 `& 'E:\hello agents_demo\.venv\Scripts\python.exe' -X utf8 scripts\check_stream.py` |
| 验证聊天入口(**约 3 次模型额度**) | 先起后端,再 `& 'E:\hello agents_demo\.venv\Scripts\python.exe' -X utf8 scripts\check_chat.py` |
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

**三种入口、一条流水线。** 表单和聊天最终都跑同一套检索 + 规划:

```
POST /api/trip/plan          → 表单直通:跑完整条链路再一次性返回
POST /api/trip/plan/stream   → 表单直通 + SSE 进度
POST /api/chat/stream        → 聊天:先判意图/抽槽位,够了再进同一条流水线
```

```
  └─ 聊天入口 services/chat_service.py
  │    (一次模型调用同时给出:意图 + 槽位 + 回复;给了 trip 就跳过这一步)
  └─ agents/trip_planner_agent.py  plan_trip_with_status() / plan_trip_stream()
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
| 聊天入口(意图 + 槽位 + 事件流) | `app/services/chat_service.py` |
| 流式进度(SSE 端点 + 前端消费) | `app/api/routes/trip.py` · `frontend/src/views/Home.vue` |
| 高德 MCP 连接与常驻 Server 生命周期 | `app/services/amap_mcp.py` |
| LLM 客户端(含 thinking 开关) | `app/services/llm_service.py` |
| 配置与凭据解析 | `app/config.py` |
| 高德结果解析 | `app/services/amap_service.py` |
| 对外契约(请求/响应模型) | `app/models/schemas.py` |
| 应用生命周期 / 路由注册 | `app/api/main.py` |

---

## 4. 进度日志

### 2026-10-16(第五轮:聊天式入口的后端,两个入口共用一条流水线)

用户要的是"聊天式"入口,**和现有表单入口并重**(不是替换),聊天里要把
LLM 返回的那些行程数据(天气 / 景点 / 酒店 / 预算…)渲染成卡片,形态类似现在的结果页。

本轮先把**后端**做完(能 pytest 全覆盖、还能真跑一遍验证),前端的聊天页与卡片组件
留给下一轮 —— 因为前端的验证条件弱(只能 build,不能目视),先把契约定下来更稳。

1. **新增 `app/services/chat_service.py`**:
   - `ChatDecision` 是**扁平**结构(意图 + 槽位 + 回复 + 缺什么),一次模型调用全给出来。
     扁平是有意的:嵌套对象正是第二轮踩过的坑(模型把嵌套对象写成 JSON 字符串,
     校验失败后原样重发,重试到 recursion_limit)。
   - 槽位拼装是**确定性**的(`build_trip_request`),模型没给的日期绝不替它编。
   - 规划完全复用 `plan_trip_stream()`,所以降级判断、超时闸门、SSE 进度都不用重写。
2. **新增 `POST /api/chat/stream`**,事件类型:`message` / `progress` /
   `artifact`(kind=`trip_plan`,带整份行程数据 + 解析出的条件)/ `done` / `error`。
3. **两个入口的接缝**:`ChatRequest.trip` 给了就直接进流水线,**跳过意图识别**,
   一次模型调用都不额外花 —— 表单页就是走这条。
4. **新增 `scripts/check_chat.py`**:两轮对话验证(先追问、补信息后出行程),
   并且会**校验天数** —— 它当场抓出了一个真 bug(见下)。

**本轮抓到的 bug(值得记下来)**:第一版提示词让模型自己推返程日期,
实测它把"三天"算成了 start+1,于是解析出 2 天、**行程真的只生成了 2 天**,
而它嘴上还说"好的,北京 3 天"(见 `check_chat.py` 第一版输出)。
根因很典型:**又让模型去做确定性代码能做的事了**。改法是把推导从模型手里拿走 ——
提示词明确"只转述、不算日期":用户说返程日才填 `end_date`,说时长只填 `travel_days`,
其余交给 `build_trip_request()` 推。改完复跑:10-24 ~ 10-26,**3 天**,与用户所说一致。

测试 95 → **122 全绿**(新增 `tests/test_chat.py`,含这个 bug 的回归用例)。

**下一轮(前端)**:`TripPlanView.vue`(只读紧凑卡片)+ `Chat.vue`(消息流 + 输入框)+ 路由。
注意:**不要再照搬 `Result.vue`** —— 它是 1410 行的整页组件,`<style scoped>` 有 480 行,
而且导出功能靠 `.attraction-card` / `.hotel-card` / `#amap-container` 这些 class 名做 DOM 查询,
搬动它会牵连导出和地图。聊天卡片按独立组件写(自己带样式),`Result.vue` 保持不动。

### 2026-10-16(第四轮:流式进度,干等 57 秒 → 第 9 秒就有反馈)

**这一轮没打算把 57 秒变短,而是把"等"变可见。** 实测耗时基本都压在那唯一一次
模型调用上,前端却要盯着一个**假的**进度条(setInterval 每 500ms 加 10%,
状态文案还是硬编码的"正在搜索景点…")干等一分钟 —— 这比慢本身更糟。

1. **后端新增 `POST /api/trip/plan/stream`(SSE)**:
   - `MultiAgentTripPlanner.plan_trip_stream()` 用 `graph.astream(stream_mode="updates")`
     拿节点级事件,每完成一个节点推一条:
     `{"type":"progress","stage","label","status","elapsed"}`;
     三路检索都回来之后再补一条 `generate_plan running`(最长的那一步);
     最后一条是 `{"type":"result",...}`,结构与原来的 `TripPlanResponse` 一致。
   - **`/api/trip/plan` 完全没动**(契约稳定),两个端点共用同一套降级判断与提示语
     (`result_message()`)。
   - 流一旦开始就不能再改 HTTP 状态码,所以中途出错是推一条 `{"type":"error"}`。
2. **前端把假进度换成真进度**(`api.ts` + `Home.vue`):
   - `generateTripPlanStream()` 用 `fetch` + `ReadableStream` 手工解析 SSE
     (axios 拿不到增量响应体);
   - 进度条按**权重**走(三个检索各 10%、生成行程 70%),并列出每个阶段的
     ✅ / ⏳ / ⚪ 与它自己的耗时;
   - 生成那 30 秒里没有事件,于是另配一个**每秒跳动的"已用 n 秒"**——
     它是真计时器,不是假进度,只是避免界面看着像死了。
3. **新增 `scripts/check_stream.py`**:真实的 SSE 客户端,给每条事件打到达时间。
   SSE 很容易被"攒到最后一起发"(反代缓冲、忘了 flush),这个脚本能在几秒内
   看出真假 —— 脚本自己会判定并给出退出码。

**实测**(北京 2 日游,`/plan/stream`):

```
  +   9.5s  进度  天气查询  done
  +  20.6s  进度  景点检索  done
  +  21.4s  进度  酒店检索  done
  +  21.4s  进度  生成行程  running
  +  39.5s  进度  生成行程  done
  +  39.5s  结果  degraded=False days=2
```

事件分散在 30 秒里 = 真流式。用户第 9.5 秒就知道天气查完了,
而不是盯着进度条猜。测试 85 → **95 全绿**(新增 `tests/test_trip_stream.py`);
`npm run build`(vue-tsc + vite)通过。

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

### P0 · 记忆系统(当前主线)

> **2026-10-16 追加**:用户要求增加**聊天式入口**(与表单入口**并重**,不是替换),
> 并且聊天里要把 LLM 返回的行程数据(天气 / 景点 / 酒店 / 预算)渲染成卡片,
> 形态类似现在的结果页。这正好落在"什么时候才需要 LLM 意图识别"那个条件上,
> 所以路由从"纯确定性"升级为"**一次调用同时产出 意图 + 槽位 + 回复**"(仍然只有一次)。
> **后端已完成**(见第五轮日志),前端待做 —— 下一步就是它。
> 记忆的三步计划不变,只是第 1 步(refine)以后可以直接接在聊天入口上。

**要解决的三件事**(其它都是"为了像记忆系统而做记忆"):

1. 会话内多轮改行程:"把第二天换成博物馆" —— 不用重填整个表单;
2. 跨会话记住偏好:"我不吃辣""带小孩" 下次规划自动生效;
3. 复用历史行程:"照上次去北京那次的风格再来一份"。

**已定的四个设计决策**(2026-10-16):

| 决策 | 结论 | 为什么不选另一个 |
| --- | --- | --- |
| 分类路由 | **要,但先用确定性的** | 意图是这个产品的**请求结构的函数**(带没带 session_id、有没有追加要求),不需要 LLM 猜。上 LLM 分类器会把刚压到 1 次的模型调用立刻变成 2 次。等做"自由聊天入口"时再补关键词兜底 + LLM 兜底 |
| 长期偏好召回 | **全量注入,不做向量召回** | 单用户偏好就几条到几十条,全塞进提示词比 top-k 更准也更可解释。"不吃辣"被向量召回漏掉是事故 |
| 历史行程召回 | **先结构化过滤**(user_id + city + 时间倒序 + 偏好交集),向量留到给不出候选时 | 现在单用户就几条历史,向量毫无收益,却要引入 embedding 模型下载 + 新依赖 |
| 记忆写入 | **复用那次规划调用回填 + 规则抽取,不新增调用**;且只记用户**显式说过**的 | 单独做"记忆抽取"会多一次模型调用;从行程内容反推偏好会让记忆自我污染、越用越偏 |

**分层**(借课程那套 working/episodic/semantic 的说法,但落到本项目实处):

- **working 工作记忆**:本次规划的中间态(三路检索结果 + 上一版 TripPlan + 对话轮次),
  按 `session_id` **精确取,不做检索**。
- **episodic 情景记忆**:历次规划(请求 + TripPlan + 时间)。结构化过滤取最近相关的几条。
- **semantic 语义记忆**:用户偏好画像。**全量注入**,并且带**出处**(是哪次输入说的)。
- **perceptual 感知记忆:不做** —— 本项目输入只有表单和文本,没有多模态来源。

**存储**:标准库 `sqlite3` 三张表(`memory_preferences` / `trip_history` / `session_state`),
**零新依赖**。不用 LangGraph checkpointer:我们的图是"表单 → 三路检索 → 规划"的
**单向流水线**,不是长驻对话图;`langgraph-checkpoint-sqlite` 还得额外装包。
等真要做多轮 agent 时再换。

**分三步落地**(每步独立可验证、独立提交):

1. **会话内 refine**:`TripRequest` 加可选 `session_id` / `user_id`(不传 = 现在的无状态行为);
   带 session_id + 追加要求就走"基于上一版改",仍然复用 `/plan` 一个端点。
2. **长期偏好**:规则抽取 + 让规划顺手回填 `detected_preferences` → 全量注入提示词;
   加 `GET/DELETE /api/memory/{user_id}` 管理接口(隐私需要);响应带 `used_memory`。
3. **情景记忆召回**:结构化过滤取最近相关行程,拼进提示词。

> RAG 知识库继续往后排(见本节末尾"明确不要做")。

### P1 · 结果级缓存 —— 已按要求标记延后

同一 (city, preferences, 日期, 住宿) 直接复用上次的 `TripPlan`(带 TTL)。
这是唯一能把**重复请求**压进秒级的手段 —— 但 2026-10-16 用户明确说
"留着以后优化的时候做",所以先标记在这里,等记忆系统做完再回来。

### P2 · 其它优化(原 P0 剩下的)

三路检索是确定性调用(0 次模型),整条链路**只剩 1 次模型调用**,而这一次就要
25~30 秒 —— 这是 ModelScope 单次生成的固有延迟。"干等"第四轮已经修掉了(SSE),
剩下的都是真耗时:

1. **缩小输出体积**:`TripPlan` 结构偏大(每天酒店 + 三餐 + 2~3 景点 + 预算)。
   若接受"预算 / 餐食由服务层兜底",模型输出能明显变短,生成时间跟着下降。
2. **换更快的模型**:同一份提示词换个更小更快的模型量一次,就知道差距有多大。
3. (可选)**把规划也做到 token 级流式**:现在 SSE 只到节点级,生成那 30 秒里
   只有计时器在动。要逐字输出就得让结构化输出改成增量解析,复杂度高不少。

> 诊断工具已经就位:`--stages tool,detail,retrieval` **完全不花额度**;
> 流式端点用 `scripts/check_stream.py` 验证(花 1 次模型额度)。

### ✅ 已完成:坐标回填(第三轮)

- `maps_text_search` 返回的 POI **不含坐标**(复测 20 条 `location` 全空),
  `maps_search_detail(poi_id)` 能拿到真坐标。
- 现在由 `app/services/trip_retrieval.py` 在**服务层固定回填**,
  不再依赖"模型记得去查";顺带把 `rating` / `opentime` / 门票也带了进来。

### P3 · 工程化

> 原本有一份 `NEXT_STEPS_CHECKLIST.md` 和一份 `BACKEND_REVIEW_AND_OPTIMIZATION.md`
> 写着更细的路线图,按用户要求已删除(历史版本仍能从 git 里翻出来)。
> 其中还值得做、且别处没记的部分收在这里:

- **结构化日志**:现在还是 `print` + emoji,`loguru` 已在依赖里;补 request id 与耗时。
- **真实依赖健康检查**:现在只查 key 存不存在,应实际 ping 一次 MCP / LLM。
- **限流 + 并发背压**:一个 `/plan` 会 fan-out 出 3 个检索 Agent,每个又多次调工具。
- **成本护栏**:按调用量 / token 统计,设单用户日配额(直接关系到额度被刷爆)。
- **接口鉴权与配额**:`/api/trip/plan` 目前无鉴权,任何人都能拿来刷你的额度。
- **可观测性**:接入 LangSmith / Langfuse,追踪工具调用、token、耗时。
- **评测集**:城市 × 偏好的 golden cases,度量 JSON 合法率、端到端时延、兜底率。
- **Docker 化**:注意镜像里得装上 `uvx`(高德 MCP 靠它拉起)。
- **密钥轮换**:`.env` 从未进过版本库,但 ModelScope / 高德 / Unsplash 的 key
  建议手动轮换一次 —— 这条原本只写在被删掉的那两份文档里,别跟着一起丢了。
- **清理配置过渡残留**:`config.py` 仍加载 `HelloAgents/.env`、保留 `openai_*` 字段。

### 明确不要做

- **RAG 知识库**:等记忆系统做完再说(先把"记住人"做扎实,再谈"记住知识")。
- **感知记忆 / 知识图谱 / Qdrant**:课程里那套完整架构对本项目过重,且没有需求来源。
- ~~在单轮请求跑通之前不要动记忆~~:**这条已经解除** —— 单轮请求 2026-10-16 跑通
  (`degraded=false`、57s、SSE 流式),现在正是做记忆的时候。

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

### 第五轮(聊天式入口后端)

```
 M backend/AGENTS.md                          # 第 1/3/4/5/6 节
 M backend/app/api/main.py                    # 注册 /api/chat 路由
 M backend/app/models/schemas.py              # 新增 ChatMessage / ChatRequest
?? backend/app/api/routes/chat.py             # 新增:POST /api/chat/stream (SSE)
?? backend/app/services/chat_service.py       # 新增:意图+槽位一次调用、事件流
?? backend/scripts/check_chat.py              # 新增:两轮对话验证(含天数回归检查)
?? backend/tests/test_chat.py                 # 新增:26 个用例
```

`pytest` **122 全绿**;真实两轮对话验证通过(先追问 → 补信息 → **3 天**行程,`degraded=false`)。

### 第四轮(流式进度,已提交)

```
 M backend/AGENTS.md                          # 第 1/3/4/5/6 节
 M README.md                                  # 接口一览 + 已知限制
 M backend/app/agents/trip_planner_agent.py   # 新增 plan_trip_stream / NODE_LABELS / result_message
 M backend/app/api/routes/trip.py             # 新增 POST /api/trip/plan/stream (SSE)
?? backend/scripts/check_stream.py            # 新增:SSE 真伪校验客户端
?? backend/tests/test_trip_stream.py          # 新增:流式事件序列与 SSE 分帧用例
 M frontend/src/services/api.ts               # 新增 generateTripPlanStream(fetch + ReadableStream 解析 SSE)
 M frontend/src/types/index.ts                # 流式事件类型
 M frontend/src/views/Home.vue                # 假进度条 → 真实阶段进度 + 已用秒数
```

`pytest` **95 全绿**;`npm run build` 通过;真实 SSE 实测事件分散在 30 秒里到达。

> 注:`frontend/src/services/api.ts` 里的 `generateTripPlan()`(非流式)保留未删 ——
> `/api/trip/plan` 端点仍在,前端暂时没有调用方。

### 第三轮(检索去模型化,已提交 `19d9124`)

```
 M backend/AGENTS.md                          # 第 3/4/5/6 节
 M README.md                                  # 架构描述与已知限制同步
 M backend/app/agents/trip_planner_agent.py   # 删掉三个检索 Agent 及其提示词
?? backend/app/services/trip_retrieval.py     # 新增:确定性三路检索
 M backend/scripts/diag_p0_latency.py         # 新增 retrieval 阶段,替换已删除的检索 Agent 阶段
?? backend/tests/test_trip_retrieval.py       # 新增:检索层离线用例
```

`pytest` **85 全绿**;真实全链路 57.5s / `degraded=false`(改造前 63.9~112.1s)。
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
