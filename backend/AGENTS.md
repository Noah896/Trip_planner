# AGENTS.md — 智能旅行规划助手(trip_planner)工作交接

> **每次开始工作前先读这个文件**;每次有实质进展后,更新第 4 节「进度日志」与第 5 节「下一步」。
> 最近更新:2026-10-19

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
| 浏览器里真验聊天页(**2~3 次模型额度**) | 起后端 + `npm run dev` + 无头 Edge(见 2.4),再跑 `scripts\check_chat_ui.py` |
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

### 2.4 前端相关的坑(2026-10-16 踩到)

- **vite dev server 只监听 IPv6**:`npm run dev` 之后 `netstat` 显示的是
  `[::1]:5173`,所以 `http://localhost:5173` 能通、`http://127.0.0.1:5173` **连不上**。
  写探活/自动化时一律用 `localhost`。
- **验证前端别只靠 `npm run build`**:它只能证明"类型和模板能编译"。
  本机没有 Playwright/Puppeteer,但 **Edge 可以做无头验证**:

  ```powershell
  # 起一个带调试端口的无头 Edge
  Start-Process -FilePath "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
    -ArgumentList @("--headless=new","--disable-gpu","--remote-debugging-port=9222",
                    "--user-data-dir=$env:TEMP\edge-codex","--no-first-run","about:blank") `
    -WindowStyle Hidden
  # 然后用 scripts\check_chat_ui.py(走 CDP)真打字、真点按钮、读回渲染结果
  ```

  用 `aiohttp` 就能连 CDP 的 WebSocket(它已在后端依赖里,**不用装 playwright**)。
  注意:`--dump-dom` 在新版 headless 下没有输出,走 CDP 的 `Runtime.evaluate` 更可靠。
- **别把 python 脚本从 PowerShell 管道喂给 `python -`**:中文会变成 `?`。
  临时脚本用 `-c` 配 here-string,或者直接写成文件跑。
- **`git commit -m` 里不要放英文双引号**:PowerShell 会把参数切开,
  报 `pathspec ... did not match`。用中文引号「」或干脆不用引号。

### 2.5 Qwen3.5 的 thinking:能流式拿到,但 LangChain 会吞掉(实测结论)

> 这一条是**探针结论**,不是功能说明 —— 相关的「深度思考」功能已按用户要求回退
> (完整实现与验证记录在 tag `backup/with-thinking-2026-10-16`,提交 `adb88f3`)。
> 之所以把结论留下来:它来之不易,而且**不记下来很容易得出相反的错误结论**。

2026-10-16 用**裸 `openai` SDK**实测(`stream=True` + `extra_body={"enable_thinking": true}`):

- 服务端**确实**按增量返回 `delta.reasoning_content` —— 单次回答拿到
  **6098 字符推理内容 + 2993 字符正文,共 1534 个 delta**。
  也就是说"像 DeepSeek 那样把思考过程流式打出来"**技术上可行**。
- **但走 LangChain 的 `ChatOpenAI` 拿不到**:`additional_kwargs` 是空的、
  推理增量块为 0。⚠️ 第一次探针就是用 LangChain 跑的,**差点得出"做不到"的错误结论**
  —— 要用这个能力必须绕开 LangChain 直接调 SDK。
- **代价**:那次调用跑了约 110 秒(推理还是英文的,而且很长)。
  真要产品化必须加闸门(回退前那版用的是 **700 字上限 + 30 秒超时,先到先停**),
  否则为了"看思考"要多等将近两分钟。
- 另外:开 thinking 时 `tool_choice=required/object` 会被服务端拒绝(400),
  即 **thinking 与 `response_format` 结构化输出不能共存于同一次调用**
  (第 4 节第二轮的日志里记过这个坑)。

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

### 2026-10-19(前端记忆回执:聊天与结果页可见)

把后端已有的 `used_memory` / `used_history` 真正展示给用户。本轮只改 UI
与流式契约,没有新增模型调用。

**本轮完成:**

1. 新增共享组件 `frontend/src/components/MemoryReceipt.vue`:
   - 历史行程显示城市、旧行程天数、历史出发日期与偏好快照;
   - 长期偏好按 `category + key` 去重后显示为标签,原文出处放在 tooltip。
2. 聊天页在行程卡片上方展示回执;打开结果页时把当前计划对应的回执写入
   `sessionStorage['tripPlanMemoryReceipt']`。
3. 表单页生成成功时也写入同一回执键;结果页统一渲染,两个入口行为一致。
4. 空回执会显式覆盖为空,避免上一份计划的记忆标签串到新行程。
5. 表单 SSE result 补 `used_history: null`,保持与普通 `TripPlanResponse`
   的结构一致;前端流式映射与类型同步。

**验证:** 后端 **193 项测试全绿**;`npm run build` 通过,仅保留既有的
大 chunk 体积警告。

**下一步:** 当前记忆系统已完整可见。下一项优先补真实鉴权,让
`user_id` 不再由客户端直接决定。

### 2026-10-19(情景记忆:历史行程结构化召回)

完成记忆三步方案的最后一步:支持"照上次北京那次再来一份"这类请求。
仍然是结构化查询,**没有向量库、没有新增模型调用**。

**本轮完成:**

1. `MemoryStore` 新增 `HistoryMemory` / `get_trip_history()`,只读取该用户的
   非降级成功行程,按时间倒序并支持城市精确过滤。
2. `memory_context.recall_trip_history()` 用确定性规则识别"上次 / 之前 / 那次 /
   再来一份"等表述:
   - 用户点名城市时,按城市与引用词的距离选最匹配的历史;
   - 没点名城市时取最近一次成功行程;
   - 无 user_id / 无历史时返回空候选,不阻断普通规划。
3. 历史候选被压缩进原有的一次意图识别调用;没有新增 LLM 调用。
4. 新增历史模板建单:复用城市、天数、交通、住宿、偏好和同城必去项;
   **旧日期绝不直接复用**。如果模型试图抄回旧日期、而用户又没有新的时间表达,
   后端会清空日期并追问。跨城市时不会带过去旧城市的必去景点。
5. 聊天 artifact / done 新增 `used_history`,前端类型增加 `HistoryUsage | null`。

**验证:** `pytest` 184 → **193 全绿**;`npm run build` 通过。

**记忆主线状态:** 工作记忆、长期偏好、情景记忆三层闭环均已完成。
下一步可做前端展示 `used_memory` / `used_history`、接入真实鉴权,
或回到延后的结果缓存 / RAG。

### 2026-10-19(长期偏好闭环:显式抽取 + 规划注入 + used_memory)

把上一轮只完成存储的 `memory_facts` 接入实际规划链路。仍然**没有新增模型调用**,
也没有从生成行程反推偏好。

**本轮完成:**

1. 新增 `app/services/memory_context.py`:
   - 从 `TripRequest.preferences` 抽取显式标签;
   - 从自由文本中只匹配少量高置信规则(不吃辣 / 素食 / 海鲜过敏 / 带小孩 /
     带老人 / 无障碍 / 节奏等),避免把"第二天换博物馆"这类一次性修改误存;
   - 规划前全量读取 active 事实并注入 `preferences`;
   - 本轮显式表达与历史值冲突时,当前请求优先;
   - 只有非降级行程才把本轮显式偏好写回 SQLite。
2. 表单 `/api/trip/plan`、流式 `/api/trip/plan/stream` 和 `/api/chat/stream`
   都接入同一套记忆逻辑;记忆读取/写入故障只打日志,不阻断正常规划。
3. `TripPlanResponse`、表单 SSE result、聊天 artifact / done 都新增
   `used_memory`。聊天回执里的 `request.preferences` 仍是用户本轮原始输入,
   不把注入值伪装成用户显式输入。
4. 聊天意图提示词补充:跨行程可复用的饮食、同行人、节奏条件必须写入
   `preferences`,但不得根据常识替用户补偏好。
5. 前端 `types/index.ts` 增加 `UsedMemoryFact`,表单流式入口会把
   `used_memory` 原样透传;页面暂不展示,只把契约补齐。
6. 新增 `tests/test_memory_context.py`,覆盖规则抽取、一次性修改排除、
   全量注入、冲突覆盖、新值取代旧事实、表单成功写回和降级不写回。

**验证:** `pytest` 175 → **184 全绿**;`npm run build` 通过;
仍为零新增依赖、零模型调用。

**下一步:** 做 `trip_history` 的结构化情景记忆召回(如"照上次北京那次");
前端目前尚未展示 `used_memory`,可在需要让用户看到"用了哪些记忆"时再接入 UI。

### 2026-10-19(长期偏好基础:事实生命周期 + 隐私管理接口)

按记忆主线完成了 `memory_facts` 的存储基础,本轮**没有新增模型调用**,也没有开始
偏好抽取或注入。

**本轮完成:**

1. `MemoryStore` 新增 `MemoryFact` / `MemoryDeletion` 数据结构和三个方法:
   - `get_facts()`:按用户读取,支持 category / key / status 过滤;
   - `upsert_fact()`:同值刷新出处,值变化时把旧记录标记为 `superseded`,
     新记录成为唯一 active,并用 `superseded_by` 保留演进关系;
   - `delete_user_memory()`:物理删除用户的偏好事实、历史行程和服务端会话。
2. SQLite 增加 active 唯一索引,保证
   `(user_id, category, memory_key)` 同时只有一条生效事实。
3. 新增 `GET /api/memory/{user_id}`:
   默认返回 active 偏好,`status=all` 可查看已被取代的历史事实。
4. 新增 `DELETE /api/memory/{user_id}`:
   返回偏好 / 历史行程 / 会话三类删除计数;会明确清空该用户全部服务端记忆。
5. 新增 `tests/test_memory_facts.py`,覆盖同值去重、值变更留痕、过滤、
   跨用户隔离、全量删除和 HTTP 契约。

**验证:** `pytest` 169 → **175 全绿**;没有调用模型,没有新增依赖。

**仍未完成(下一步):** 从用户明确表达中抽取偏好并调用 `upsert_fact()`;
规划前全量读取并注入 active 偏好;响应返回 `used_memory`。
`trip_history` 的情景记忆召回仍排在偏好闭环之后。

### 2026-10-19(RAG 方案已保存,暂不实施)

用户要求为系统增加 RAG 能力,但明确“先保存方案,晚点再做”。本轮只做设计归档,
没有新增依赖、没有修改运行时代码,也没有调用模型或运行端到端验证。

**已保存:** 根目录新增 `RAG_PLAN.md`,内容包括公共旅行知识 RAG 的边界、与用户
记忆的分工、文档/chunk 模型、混合检索、存储选型、LangGraph 接入点、响应契约、
评测指标、安全治理和分阶段实施路线。

**已确定的设计方向:**

1. RAG 只补充公共旅行知识,不替代高德实时 POI / 天气 / 酒店数据;
2. RAG 作为第四条检索链路与现有三路并行,规划阶段仍只有一次模型调用;
3. 第一版优先 SQLite + FTS/关键词 + embedding 混合检索,暂不引入独立向量数据库;
4. 用户记忆继续走现有 SQLite 结构化存储,不进入公共向量索引;
5. RAG 失败只标记 `knowledge_degraded`,不让整个行程变成降级占位结果;
6. 聊天知识问答会破坏当前“一次调用同时判意图并流式回复”的结构,暂不纳入第一版。

**状态:** 方案已归档,等待用户后续启动。

### 2026-10-18(记忆模块第一步:SQLite 工作记忆 + 会话内 refine)

按用户确认的路线,**先不上向量库**:短期工作记忆与未来的长期结构化记忆都落在
SQLite;向量检索等历史行程/偏好规模真的需要时再加。

**本轮完成:**

1. 新增 `app/services/memory_store.py`:
   - `session_state` 保存 `base_request`(首次稳定请求)、当前 `request`、最新
     `TripPlan`、版本号、更新时间与 `last_message`;
   - `trip_history` 同时落一条成功行程,后续情景记忆召回直接复用;
   - `memory_facts` 表结构已建好,但本轮**还没有**偏好抽取/注入闭环;
   - 原生 `sqlite3`、WAL、5 秒 busy timeout,每次操作独立连接;
   - 默认 `data/trip_memory.sqlite3`(已加入 `.gitignore`),会话 TTL 默认 30 天;
   - `user_id` 非空时会校验会话归属,不匹配抛 `MemoryAccessError`。
2. `TripRequest` / `ChatRequest` 新增可选 `session_id` / `user_id`;
   不传时行为与以前完全一致(无状态)。
3. 聊天入口新增 `intent=refine`:
   - 上一版请求/行程会先被压缩进意图识别上下文;
   - 城市、日期、天数等未改槽位由 `merge_refinement()` 确定性保留,
     不依赖模型把旧槽位重新抄一遍;
   - 当前修改写进 `free_text_input`,`base_request` 不会被多轮修改污染;
   - 规划提示词会收到上一版每日景点/酒店摘要,明确“未要求改变的部分保留”。
4. 只有 `degraded=false` 的计划才写记忆;降级占位行程不会污染历史。
5. `/api/trip/plan`、`/api/trip/plan/stream` 和 `/api/chat/stream`
   都支持保存成功行程并返回 `memory_saved` / `memory_version`。
6. 前端新增 `services/session.ts`:浏览器用 `localStorage` 生成稳定
   `session_id` / `user_id`,聊天页与表单页都会携带。接入登录后,
   `user_id` 必须改为鉴权层提供。

**验证:**

- `pytest` 158 → **169 全绿**;
- `npm run build` 通过;
- 新增 `tests/test_memory.py`,覆盖保存/读取、用户归属、refine 合并、
  降级不写入、保留 base request、连续多轮修改以最新版本为基座、
  旧会话不存在时追问。

**仍未完成(下一步):** `memory_facts` 的显式偏好抽取、全量注入与
`GET/DELETE /api/memory/{user_id}` 管理接口;`trip_history` 的
“照上次北京那次”类结构化召回;向量索引明确暂不做。

### 2026-10-16(用户说"没解决" —— 真正的原因:我改了代码但没重启后端)

用户回来说"还是一样的问题,没有解决"。查下来**不是我改的代码没用,是我没让它生效**:

```
后端进程启动时间 = 22:21:22
chat_service.py  = 22:42:53   ← 比后端启动晚了 21 分钟
```

`run.py` 默认关着热重载(`UVICORN_RELOAD=0`),所以改了文件不重启就一直是旧代码。
用户浏览器连的那个进程里,还是那四条**写死的示例** —— 他看到的当然是"还是那几个"。

而我这边的"验证通过"是在**进程内**跑的探针:它 `import` 的是磁盘上的新代码,
所以显示正常。**进程内探针只能证明"逻辑对",证明不了"服务在跑它"。**

> **教训(比这次两个 bug 都重要)**:验证必须在**用户实际使用的那条路径**上做。
> 以后凡是改了后端代码:①先重启;②核对"进程启动时间 > 文件修改时间";
> ③再走真实 HTTP 验证。第 1 节里本来就写着这套姿势,是我没照做。

**顺带修掉两个真 bug**(都是这次排查暴露的):

1. **回复里出现字面的 `\n`**:模型偶尔把换行写成**双重转义**(JSON 里是 `\\n`),
   解出来是 `\` + `n` 两个字符,用户看到的是字面的 `\n`。4 次请求里出现过。
   修法:`_normalize_escapes()` 把字面量还原成真换行/制表符。
2. **回复被截断**(更严重,而且是我修 1 时自己引入的):给流式加"必须以前缀追加"的
   保护后,双重转义在流式解析里会**短暂解出一个悬空的反斜杠**并先发出去;
   等真正的换行到达,文本不再以它为前缀 → 后面全被挡住,用户看到
   `…推荐：\` 就没了。修法两道:
   - 流式时**扣住尾部可能不完整的转义序列**(末尾是 `\` 就先不发);
   - 对齐时按**最长公共前缀**补齐,保证整句完整送达(撤不回已发出的,但至少不缺尾巴)。

**实测**(重启后端后走真实 HTTP 连问 4 次):4 份回复内容都不同,
没有字面 `\n`,没有截断。

测试 156 → **158 全绿**。

### 2026-10-16(修复:推荐永远是那几个 —— 我写死的示例被当成了答案)

**用户报的现象**:"我让他给我推荐几个景点,为什么永远是那几个"。

**根因是我上一轮干的**:为了修"复读机",我在提示词里写了**四条具体景点**
当格式示例:

```
- 北京 · 故宫博物院 —— 想看明清皇宫和中轴线,历史感最强
- 西安 · 兵马俑 —— 想看震撼的考古现场
- 成都 · 大熊猫繁育研究基地 —— 带小孩或想轻松一点
- 杭州 · 西湖 —— 想慢逛、拍照、吃本地菜
```

模型**把示例直接当成答案照抄**了,于是不管谁来问,推荐永远是这四个。
**示例本身变成了内容** —— 这和"规则被过度推广"(上一轮那个复读机)是同一类毛病:
提示词里写得太具体的东西,会被模型当成结论而不是参考。

**修法**:

- 新增 `RECOMMENDATION_POOL`(16 个中国大陆城市,刻意混入泉州 / 大同 / 景德镇 /
  洛阳 / 敦煌这类不那么烂大街的地方);每次请求**随机抽 3 条**塞进提示词。
- 把这 3 条定位成"**本次候选池,优先从里面挑**"(而不是"格式示例"),
  并写明"每次都不同,绝对不要反复给同样的几个地方"。
- 示例每次都变 → 就算照抄,也不会重复。

**实测**(同一句话"给我推荐几个景点"连问三次):

```
1: 杭州·西湖 / 长沙·岳麓山 / 厦门·鼓浪屿 / 西安·兵马俑
2: 重庆·洪崖洞 / 大同·云冈石窟 / 敦煌·莫高窟
3: 景德镇·陶溪川 / 北京·故宫 / 西安·兵马俑
```

只有"西安兵马俑"重复了一次(它是池子里的常驻名角,模型的惯性也在)。
中途我还改坏过一次格式(模型把四条挤成一整段),补回"每个占一行、以 `- ` 开头、
**不要挤成一整段**"之后恢复正常。

**教训(同一类坑的第二次)**:提示词里的**具体内容会被当成答案**。
所以 —— 规则要写清作用域,**例子要每次都不一样**;光写"不要照抄示例"是没用的。

### 2026-10-16(聊天回话改成流式:边生成边推)

用户要"像 DeepSeek 那样把回复流式打出来"。做法选的是**不加模型调用**的那条:
意图识别本来就是一次结构化输出(工具参数就是 JSON),而 `reply` 是**第 2 个字段**
—— 那就边收工具参数的增量、边把 `reply` 的值抽出来推给前端。

关键实现:

- 不再用 `create_agent` 跑意图识别,改成 `get_llm().bind_tools([ChatDecision],
  tool_choice="required")` 直接流式调用。**这顺带从根上去掉了递归上限那个坑**:
  以前 create_agent 在结构化输出校验失败时会**让模型重试**,模型把同样参数原样再发,
  一路撞到 recursion_limit(实测撞出过 GraphRecursionError);现在只调一次、
  自己解析,失败就直接退化成追问,没有循环可言。
- `extract_reply()` 从**还没写完的 JSON** 里抠出 `reply` 的当前值
  (末尾可能是半个转义序列,去掉再试)。中文、`\"`、`\n` 都验过。
- 结束时会**对齐**:最终 `decision.reply` 比已经流出去的长,就把差额补上;
  短了就以最终为准(并打一条日志)。用户不会看到半句话,也不会重复。
- 前端 `message` 事件从"换行拼接"改成**增量追加**(以前一次就是整句,不会暴露这个问题)。

**实测**(无头浏览器采样助手气泡的文本长度):

```
"你好"(回复 48 字):        [0 ×12, 9, 48, 48]          2 段,约 0.4 秒到齐
"给我推荐几个不同类型的景点"(162 字): [0 ×4, 6, 67, 156, 162]  4 段,约 0.75 秒到齐
```

结论:**流式是真的,但分块粒度取决于服务端** —— ModelScope 一次给几十个字符,
所以看起来是"跳几下",不是平滑逐字。

> **刻意没做的一件事**:前端加个"打字机"动画把收到的字慢慢吐出来 —— 那能做出
> 平滑效果,但它和刚删掉的**假进度条**是同一类东西(用动画冒充真实进度)。
> 宁可让用户看到真实的分块,也不要再造一个"看起来在动"的假象。

测试 146 → **152 全绿**(新增:分块流式、转义解码、截断 JSON 抽取、
失败时兜底话术也要流出去、run_chat 多条 message 拼起来等于整句)。

### 2026-10-16(修复:用户点名的景点变成硬约束 + 不再自己编出发日期)

**用户报的现象**:他让助手推荐景点,选了一个,助手问玩几天,他答"3 天" ——
结果出来的行程里**第一天是他选的、后两天全是系统自己挑的**。
他还问:"这个问题能不能用 ReAct 范式解决?"

**根因不是"模型不会推理",而是"他选的东西根本没变成数据"**:

- `ChatDecision` 里**没有**"用户点名要去的地方"这个字段 —— 他选的景点最多以
  自由文本(额外要求)的形式勉强传下去;
- `retrieve_attractions()` 只按 `preferences` 搜,**压根不知道他选了故宫**;
  于是候选池是"按偏好搜出来的一堆",规划自然就从里面随便排。
- 顺带还有第二个口子:用户没说日期时,**模型自己挑了个出发日**就开跑了
  (所以"只回答 3 天"也能直接出行程)。

**修法**(把选择变成约束,而不是提示):

1. `ChatDecision` / `TripRequest` 新增 **`must_visit: List[str]`**;
   提示词里写死"用户点名要去的地方**必须填进 must_visit**,不能漏"。
2. `retrieve_attractions()`:**先按名字解析必去景点**,把它们放在候选池最前面,
   再去重、再补偏好搜索结果。按名字搜不到就退回**地理编码**拿坐标
   (口语简称如"故宫"也能落地),这样"用户要的"一定有真实坐标,不靠模型编。
3. `PLANNER_AGENT_PROMPT` + 规划查询:明确"用户指定必去的景点必须安排进行程,且优先"。
4. 提示词:用户**完全没提时间**时,`start_date`/`end_date` 一律留空、由后端追问,
   不许自己挑一个日期。

**实测**(走真实 HTTP 链路):"北京玩3天,下周末出发,就去故宫吧" →

```
第1天 2026-10-24: 故宫博物院、景山公园        ← 点名的排第 1 天
第2天 2026-10-25: 天安门广场、毛主席纪念堂、中山公园
第3天 2026-10-26: 天坛公园、北海公园
```

比修复前明显更像"北京三日游"(之前是南苑森林湿地公园、千灵山这类冷门点位)。

**关于"能不能用 ReAct 解决"(用户问的)**:不能,这是用错刀。

- ReAct 解决的是**"下一步该做什么"**(让模型自己决定调哪个工具、循环到满意),
  而这里的毛病是**"用户的选择没有传下去"** —— 模型再会推理,它手里也没有
  "用户选了故宫"这个事实。**数据传递的 bug 不能靠推理范式修。**
- 代价也很具体:我们现在这条链路是把 ReAct 式检索 Agent **换掉**之后才降到
  1 次模型调用的(原来模型自己决定调用、一口气发 20 个 detail、还编坐标)。
  换回 ReAct 等于把这些代价重新引入。
- **ReAct 真正合适的地方**是"下一步取决于上一步结果"的探索:比如必去景点当天闭馆
  要临时换、小城市候选太少要换关键词再搜一轮。那时也该**限定在检索层、
  设死轮数与耗时上限**,而不是把整条链路交给模型。

**顺带记一个未解释的现象**:排查时写过一个**进程内**探针(直接调
`chat_service.run_chat`),它卡了 12 分钟没返回;而**同样逻辑走 HTTP 只要 73 秒**。
可能是探针自己的问题(它在脚本进程里自己拉 MCP Server),但有个真实隐患值得注意:
`plan_trip_stream()` 里 `_ensure_graph()` 在 `asyncio.timeout` **外面**,
而且图内部的取消不一定能被 `asyncio.timeout` 打断 —— 真要死锁,240 秒的闸门可能不生效。

### 2026-10-16(修复:把"范围约束"收窄,别让助手变成只会追问的复读机)

**用户拿截图问"你不觉得有什么问题吗"。** 对话是这样的:

```
你好                    → 你好！我是智能旅行规划助手…你想去哪里玩呢?
给我推荐一个景点          → 本服务主要提供中国大陆的行程规划,需要您指定一个城市…
你随便给我推荐一个景点     → 本服务的数据源只覆盖中国大陆,需要您指定一个城市…
```

三个问题,而且都是**上一条"修复"引起的**:

1. **重复追问**:用户明确说了"随便"(= 我不想选,你替我定),助手却第三次要城市。
2. **规则外泄**:从头到尾没人提境外,它却把"数据源只覆盖中国大陆"念出来当挡箭牌。
3. **丧失帮忙的能力**:第一句"给我推荐一个景点"其实是个很轻的请求,直接给两个例子
   就够了,它却把它当成"缺参数、无法执行"。

**根因是我上一轮写的那条规则**:

```
3. 用户没说城市时,不要自己挑一个城市(更不要挑一个境外的),应该追问。
```

我本意是拦住"模型凭空挑个城市然后闷头跑 60 秒规划",但模型把它推广成了
**"没有城市就什么都不说"** —— 约束的**作用域**没收窄,把"闲聊推荐"也一起限死了。

**修法**(`CHAT_SYSTEM_PROMPT`):

- 明确作用域:**只有 `intent=plan`(生成行程)才要求用户给出城市**;
  "只是聊天 / 让你推荐"不在此列,没城市也要照常给建议。
- 新增「用户把选择权交给你时」一节:说"随便/都行/你定"时**不要再问同一个问题**,
  直接给 2~4 个具体选项(`城市 · 景点 —— 一句话为什么`,覆盖不同类型),让他挑。
- 新增一条:**不要把自己的规则念给用户听**,只在确实需要时说一次。

**实测**(同一轮对话,重启后端后):

```
你随便给我推荐一个景点
  → 没问题，给您推荐几个不同类型的热门景点：
    - 北京 · 故宫博物院 —— 想看明清皇宫和中轴线，历史感最强
    - 西安 · 兵马俑 —— 想看震撼的考古现场
    - 成都 · 大熊猫繁育研究基地 —— 带小孩或想轻松一点
    - 杭州 · 西湖 —— 想慢逛、拍照、吃本地菜
    想排成完整行程的话，告诉我选哪个、大概什么时候去。
```

**教训(这轮最值钱的一条)**:写约束时要写清**作用域**。
"不要让模型编城市"和"不要让模型拒绝帮忙"只有一线之隔 ——
前者是限制**执行**,后者是限制**回应**,混在一起就会得到一个礼貌但没用的复读机。

### 2026-10-16(修复:默认只推荐国内 + 意图识别容错)

**用户报的现象**:让助手推荐景点,它推荐了一个**日本**的景点;用户强调
"没有特别说明的话我只要国内的景点"。

**先说诚实的部分:我没能复现。** 连着试了五种说法
("推荐一些景点"/"给我推荐几个景点"/"推荐一下秋天适合去的景点"/
"推荐适合带孩子去的景点"/"推荐几个主题乐园"),模型要么追问城市、
要么推荐的确实是国内景点(香山、西湖、明孝陵)。所以**具体触发路径还不确定**
(很可能是那句原话的措辞、或者当时对话历史里的上下文)。

不过排查时有**两个确定的问题**,都修了:

1. **没有任何范围约束** —— 闲聊式推荐(intent=chat)是模型凭自己知识自由发挥,
   提示词里从没说过"默认只做国内"。这是个真实的口子:只要话题够泛
   (红叶、亲子、主题乐园),它就可能把京都、东京迪士尼这类内容带出来。
   修法:
   - `CHAT_SYSTEM_PROMPT` 增加**范围约束**三条:默认只推荐中国大陆;
     用户明确要求境外时**不要凭记忆推荐**,要如实说明高德数据只覆盖中国大陆;
     没说城市时**不要自己挑一个**(更不要挑境外的)。
   - `PLANNER_AGENT_PROMPT` 增加两条:**只能用上面提供的信息**
     (不得用记忆添加/替换景点、酒店、餐饮),以及默认中国大陆。
2. **意图识别会因为结构化输出失败而撞死**(排查时顺带撞出来的)。
   `"给我推荐几个景点"` 直接抛 `GraphRecursionError: Recursion limit of 12` ——
   和**第二轮**规划 Agent 那个死循环是同一个病根:某个字段校验失败 →
   LangChain 让模型重试 → 模型把同样的参数原样再发一遍 → 撞到上限。
   修法两道:
   - `ChatDecision` 补容错校验:`travel_days` 接受 `"3 天"`/`"三天"`/`3.0`
     (读不出来就置 None,交给"信息不全→追问");`intent` 归一化大小写与空格。
   - `decide()` **不再向上抛错**:失败时退化成一次追问
     ("抱歉,我刚才没太理解……"),而不是把整轮对话变成一条报错 ——
     用户看不懂那个错误,也没法处理。

**实测**(重启后端后):

```
用户: '推荐东京的景点'
  intent = 'chat'
  reply  = '抱歉，本服务的数据源只覆盖中国大陆，无法提供东京等境外景点的真实数据。
            您是否想换成国内的城市，比如北京、上海或西安？'

用户: '给我推荐几个景点'      ← 之前直接崩在这里
  intent = 'clarify'
  reply  = '没问题！不过您打算去哪个城市玩呢？另外，您计划什么时候出发、玩几天呀？'
```

测试 122 → **137 全绿**(新增范围约束断言、字段容错、decide 退化用例)。

**待补**:如果还能拿到用户当时的那句原话,拿它复跑一遍才能确认范围约束是否真的
盖住了那个具体路径。

### 2026-10-16(修复:只发「你好」也像在生成计划,然后"卡住")

**用户报的现象**:在聊天页只发一句"你好",界面也摆出"生成计划"的样子,然后一直不动。

**排查**:先写探针直接调 `chat_service.decide()`,三种场景(无历史 / 聊到一半 /
已经聊全)下"你好"都被正确判成 `intent='chat'`、`city=None` —— **后端是对的**。
于是回头看前端,一眼就看到了:

```ts
// 旧代码:不管这轮会不会规划,先把四个阶段摆成"等待中"
messages.value.push({
  role: 'assistant', text: '',
  progress: STAGE_LABELS.map((label) => ({ label, status: 'pending' }))
})
```

闲聊那一轮后端**不会发任何进度事件**,于是那四行永远停在 ⚪ ——
看起来就是"它要去生成计划,但卡住了"。用户看到的不是慢,是**假进度**。

**修法**(两处):

1. **前端改成按事件惰性建清单**:有 `progress` 事件才加行,不再预置。
   行按固定顺序插入(三路检索是并行的,事件到达顺序不固定,否则每次刷新都在跳)。
2. **后端在开始检索时补发三条 `running`**:三路检索本来就是并行开始的;
   以前要等第一个节点跑完(实测 7~38 秒)才发第一条进度,中间那段全是空白。

**实测**(无头浏览器):

- 只发"你好" → 助手回复正常,`progress-row` 数量 **0**、没有行程卡片;
- 正常规划 → **+15s** 就能看到三路都是 ⏳(改之前要等到 +24s),
  随后逐个 ✅、生成行程、卡片渲染,全链路正常。

**教训**:进度 UI 的初始化不能**先于**"这轮确实要执行"这个事实。
宁可晚半秒出现,也不要给一次不会发生的执行画进度条 ——
用户会把它当成"卡住",而且这种假象比慢本身更难排查(会让人以为是后端在跑)。

> 附带发现:即使判成 `chat`,这一轮也要花一次意图识别调用,实测 5~25 秒
> (同一句"你好"在不同时间差别很大)。如果嫌慢,可以给"明显的问候"加一条
> **确定性快路**(不调模型直接回一句),但那是另一个决定了。

### 2026-10-16(第六轮:前端聊天页,两个入口并重)

1. **新增 `frontend/src/views/Chat.vue`**(聊天页,路由 `/chat`):
   - 消息流:用户气泡 / 助手气泡 / **进度清单**(⚪ 等待 → ⏳ 进行中 → ✅ 完成 + 各自耗时)
     / **行程卡片**;空态给三个引导词,点一下填进输入框。
   - 进度清单一开始就把四个阶段按"等待中"列出来 —— 否则前 10~20 秒界面看着像死了。
   - 拿到 artifact 后可以把整份行程塞进 `sessionStorage` 并跳到结果页
     (结果页的编辑 / 导出 / 地图都还在,聊天页不重复实现)。
2. **新增 `frontend/src/components/TripPlanView.vue`**:只读紧凑行程卡片
   (概览 / 天气 / 每日景点酒店餐饮 / 预算),自带 scoped 样式。
   **特意没有去改 `Result.vue`**:它是 1410 行的整页组件,`<style scoped>` 有 480 行,
   而且导出功能靠 `.attraction-card` / `.hotel-card` / `#amap-container` 这些 class 名
   做 DOM 查询 —— 搬动 markup 会牵连导出与地图,而前端又没法目视回归。
3. **两个入口并重**:首页表单下方加了「不想逐项填表?直接说一句就行 → 和助手聊聊」,
   表单页本身一行未动。
4. **`api.ts` 重写**:把 SSE 解析抽成共用的 `readSSE()`,新增 `chatStream()`
   (两个 POST 端点都不能用 `EventSource`,它只支持 GET)。

**验证方式(这轮换了打法)**:本机没有 Playwright,但用**无头 Edge + CDP** 真跑了一遍 ——
新增 `scripts/check_chat_ui.py`:开浏览器、在输入框里模拟输入并派发 `input` 事件
(Vue 的 v-model 靠这个才更新)、点发送、轮询 DOM、把渲染结果读回来。实测:

```
✅ 页面挂载成功: /chat  💬 直接和助手说你想怎么玩
   +15s  ⚪ 景点检索 / ✅ 天气查询 9.2s / ⚪ 酒店检索 / ⚪ 生成行程   ← 进度是真的在动
   +33s  ✅ 景点检索 26.2s / ✅ 天气查询 9.2s / ✅ 酒店检索 26.2s / ⏳ 生成行程
   🤖 好的，北京3天，下周末（10月24日）出发，带6岁小孩，我这就为您规划亲子行程。
📦 🗺️ 北京 3 日游 | 2026-10-24 ~ 2026-10-26 | 预估 ¥2680 | 3 天天气 | 每天景点/酒店/三餐
```

另外用 CDP 复查了首页:`.chat-entry` 存在、提交按钮和表单都还在(改 Home.vue 的回归检查)。
`pytest` 122 全绿;`npm run build`(vue-tsc + vite)通过。

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

### P0 · 记忆系统(三层闭环已完成)

> **2026-10-19 进度**:第 1 步“服务端会话 + refine”已完成并提交
> (`71a0bc1`);`memory_facts` 的事实生命周期与
> `GET/DELETE /api/memory/{user_id}`、显式偏好抽取、规划前全量注入和
> `used_memory` 回执均已完成;`trip_history` 的结构化情景记忆也已完成,
> 支持“照上次北京那次”并返回 `used_history`。工作 / 长期偏好 / 情景三层闭环
> 均已完成,前端聊天 / 结果页也已展示记忆回执,**仍然不做向量库**。
> 下一阶段优先补真实鉴权。

> **2026-10-16 追加**:用户要求增加**聊天式入口**(与表单入口**并重**,不是替换),
> 并且聊天里要把 LLM 返回的行程数据(天气 / 景点 / 酒店 / 预算)渲染成卡片,
> 形态类似现在的结果页。这正好落在"什么时候才需要 LLM 意图识别"那个条件上,
> 所以路由从"纯确定性"升级为"**一次调用同时产出 意图 + 槽位 + 回复**"(仍然只有一次)。
> **前后端都已完成**(见第五、六轮日志)。
>
> **还差一步才算"有记忆"**:现在对话历史是**客户端每轮带上来**的
> (`ChatRequest.history`),服务端不存;而且用户说"第二天换成博物馆"时,
> 后端只会当作新需求重新抽槽位、重新规划 —— **没法基于上一版改**。
> 所以下一步就是记忆三步方案里的第 1 步:**服务端会话 + refine**。

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

### P0.5 · 公共旅行知识 RAG —— 方案已保存,暂缓实施

> **2026-10-19 状态**:用户要求先保存方案、晚点再做。完整设计见
> `RAG_PLAN.md`。当前不新增依赖、不改运行时代码,也不打断记忆系统主线。

启动时按以下顺序推进:

1. 先确定首批城市/景区知识范围和来源白名单,准备 30~50 个评测问题;
2. 做 SQLite 关键词 + embedding 混合检索 MVP,暂不引入独立向量库;
3. LangGraph 增加与三路实时检索并行的 `search_knowledge` 节点,规划仍只调用一次模型;
4. 响应增加服务层生成的 `knowledge_sources`,不让模型自行编造引用;
5. RAG 无命中、超时或索引失败不得阻断主流程,只标记 `knowledge_degraded`;
6. 等规划链路稳定后,再评估聊天知识问答、reranker 和 Qdrant/pgvector。

**明确边界:** 高德仍是坐标、天气、酒店等动态事实的权威来源;用户记忆继续使用
SQLite 结构化存储,不写入公共知识向量索引。

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

### 修复:改完代码没重启后端 + 双重转义(2026-10-16)

```
 M backend/AGENTS.md                      # 第 4 / 6 节
 M backend/app/services/chat_service.py   # _normalize_escapes + 扣住半个转义序列 + 公共前缀补齐
 M backend/tests/test_chat.py             # +2 用例
```

`pytest` **158 全绿**。重启后端后走真实 HTTP 连问 4 次:内容都不同、无字面 `\n`、无截断。

> 这一轮真正的"修法"是**重启服务** —— 代码早就对了,只是没生效。教训见第 4 节。

### 修复:推荐永远是那几个(2026-10-16)

```
 M backend/AGENTS.md                      # 第 4 / 6 节
 M backend/app/services/chat_service.py   # RECOMMENDATION_POOL + build_system_prompt 随机抽示例
 M backend/tests/test_chat.py             # +4 用例
```

`pytest` **156 全绿**。实测同一句话连问三次,推荐内容三次都不同。

### 聊天回话改成流式(2026-10-16)

```
 M backend/AGENTS.md                        # 第 4 / 6 节
 M backend/app/services/chat_service.py     # bind_tools 直调 + extract_reply + decide_streaming + 对齐
 M backend/app/api/routes/chat.py           # 说明 message 是增量
 M backend/tests/test_chat.py               # +6 用例
 M frontend/src/views/Chat.vue              # message 改为增量追加
 M frontend/src/types/index.ts              # 注明 text 是增量
```

`pytest` **152 全绿**;`npm run build` 通过;无头浏览器实测文本是逐步增长的。
**没有增加任何模型调用。**

### 修复:用户点名的景点变成硬约束(2026-10-16)

```
 M backend/AGENTS.md                          # 第 4 / 6 节
 M backend/app/models/schemas.py              # TripRequest.must_visit
 M backend/app/services/chat_service.py       # ChatDecision.must_visit + 两条提示词规则
 M backend/app/services/trip_retrieval.py     # 必去景点优先解析 + 地理编码兜底
 M backend/app/agents/trip_planner_agent.py   # 规划提示词/查询里的必去约束
 M backend/tests/test_trip_retrieval.py       # +4 用例
 M backend/tests/test_chat.py                 # +3 用例
```

`pytest` **146 全绿**。真实 HTTP 实测:点名的故宫排在第 1 天,后两天也是正经地标。

### 修复:别让助手变成只会追问的复读机(2026-10-16)

```
 M backend/AGENTS.md                      # 第 4 / 6 节
 M backend/app/services/chat_service.py   # 范围约束收窄 + 「用户说随便」的处理方式 + 别念规则
 M backend/tests/test_chat.py             # 新增 3 个提示词断言
```

`pytest` **140 全绿**。实测:同一轮对话从"第三次追问城市"变成给出 4 个具体选项。

### 修复:默认只推荐国内 + 意图识别容错(2026-10-16)

```
 M backend/AGENTS.md                          # 第 4 / 6 节
 M backend/app/agents/trip_planner_agent.py   # 规划提示词:只用检索结果 + 默认中国大陆
 M backend/app/services/chat_service.py       # 范围约束三条 + 字段容错 + decide 退化成追问
 M backend/tests/test_chat.py                 # +15 用例
```

`pytest` **137 全绿**。实测:要求东京景点会如实说明范围并建议换国内城市;
之前会 GraphRecursionError 的那句现在正常追问。

> ⚠️ 用户报的"推荐了日本景点"**没有复现出来**(试了五种说法),这里是按
> "补上缺失的范围约束"来修的,不是按复现路径修的。见第 4 节那条的说明。

### 修复:闲聊轮不该画进度条(2026-10-16)

```
 M backend/AGENTS.md                          # 第 4 / 6 节
 M backend/app/agents/trip_planner_agent.py   # 开始检索时补发三条 running
 M backend/tests/test_trip_stream.py          # 事件序列断言随之更新
 M frontend/src/views/Chat.vue                # 进度清单改为按事件惰性创建 + 固定顺序
```

`pytest` **122 全绿**;无头浏览器实测:"你好" → 0 行进度 + 只有文字回复;
正常规划 → +15s 就能看到三路 ⏳(改前要等 +24s)。

### 回退「深度思考」+ 保留实测结论(2026-10-16)

用户试用后选择回到 `9823a7a`(只撤掉「深度思考」,聊天页保留),已执行
`git reset --hard 9823a7a`,并留下保险 tag `backup/with-thinking-2026-10-16`(`adb88f3`)。
撤掉的内容:`app/services/thinking.py`、`tests/test_thinking.py`、`ChatRequest.thinking`、
`plan_trip_stream` 的 `thinking` 参数、前端开关与思考过程块。测试 131 → **122 全绿**。

**但把探针结论单独留在了第 2.5 节**(纯文档,不含任何功能代码)——
第一次探针用 LangChain 跑,结论是"零推理块",差点误判成"平台不支持";
这种坑不写下来下次还会重走一遍。

### 第六轮(前端聊天页)

```
 M backend/AGENTS.md                             # 第 1/2/4/6 节
 M frontend/src/main.ts                          # 新增 /chat 路由
 M frontend/src/services/api.ts                   # 抽出 readSSE(),新增 chatStream()
 M frontend/src/types/index.ts                    # 聊天事件类型
 M frontend/src/views/Home.vue                    # 表单页下方加聊天入口
?? frontend/src/views/Chat.vue                    # 新增:聊天页
?? frontend/src/components/TripPlanView.vue       # 新增:只读行程卡片
?? backend/scripts/check_chat_ui.py               # 新增:无头 Edge + CDP 的 UI 验证
```

`npm run build` 通过;无头浏览器里真跑通了「输入 → 事件流 → 行程卡片」;
首页表单回归检查通过。`Result.vue` **一行未动**。

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
