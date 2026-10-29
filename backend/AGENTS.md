# AGENTS.md — 智能旅行规划助手(trip_planner)工作交接

> **每次开始工作前先读这个文件**;每次有实质进展后,更新第 4 节「进度日志」与第 5 节「下一步」。
> 最近更新:2026-10-29

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

**鉴权提醒**:账号鉴权默认开启。业务接口需要
`Authorization: Bearer <token>`;验证脚本可设 `TRIP_AUTH_TOKEN`,或同时设
`TRIP_AUTH_USERNAME` / `TRIP_AUTH_PASSWORD` 自动登录。前端首次使用先注册账号。

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
            ├─ query_weather      ─┤
            ├─ search_hotels      ─┼─ 四路并行
            ├─ search_knowledge   ─┘
            │     ↑ 景点/天气/酒店是 services/trip_retrieval.py 的确定性调用
            │       (直接调高德 MCP + 服务层解析,**0 次模型**)
            │     ↑ 知识检索走 app/rag/:BGE + FAISS + BM25 + RRF,**0 次模型**
            └─ generate_plan       → **唯一的模型调用**:response_format=TripPlan 结构化输出
                 失败 → planner_fallback + extract_json
      核心实时检索/规划失败 → _degrade();RAG 失败只标记 knowledge_degraded
```

| 关注点 | 文件 |
| --- | --- |
| LangGraph 编排 / 规划 Agent / 兜底 | `app/agents/trip_planner_agent.py` |
| 三路实时检索(确定性,不经过模型) | `app/services/trip_retrieval.py` |
| 公共知识 RAG(BGE + FAISS + BM25 + RRF) | `app/rag/` |
| 聊天入口(意图 + 槽位 + 事件流) | `app/services/chat_service.py` |
| 流式进度(SSE 端点 + 前端消费) | `app/api/routes/trip.py` · `frontend/src/views/Home.vue` |
| 高德 MCP 连接与常驻 Server 生命周期 | `app/services/amap_mcp.py` |
| LLM 客户端(含 thinking 开关) | `app/services/llm_service.py` |
| 配置与凭据解析 | `app/config.py` |
| 高德结果解析 | `app/services/amap_service.py` |
| 对外契约(请求/响应模型) | `app/models/schemas.py` |
| 应用生命周期 / 路由注册 | `app/api/main.py` |
| 登录失败限流 | `app/services/auth_service.py` · `app/api/routes/auth.py` |
| 按用户模型额度 | `app/services/model_quota_service.py` · `app/api/quota_dependencies.py` |
| 账号改密 / 禁用 / 一次性找回 / 审计 | `app/services/auth_service.py` · `app/api/routes/auth.py` |
| 历史行程浏览 / 详情 / 删除 | `app/api/routes/history.py` · `frontend/src/views/History.vue` |
| 旅行规划质量评测 | `evals/evaluator.py` · `scripts/eval_trip_plans.py` |

---

## 4. 进度日志

### 2026-10-29(修复:高德“查看信息”回落到当前地址)

用户点击景点“查看信息”后,高德网页只定位到当前地址。实测旧链接
`https://www.amap.com/place/{poi_id}` 会跳到 `/ssr/place/{poi_id}`,随后
`getPoiInfo` 请求命中高德网页风控;景点信息获取失败后,页面回落到 IP 所在地。
`/detail/{poi_id}` 同样会被验证码拦截。上一轮只验证了前端生成的 href,没有验证
高德页面最终定位,因此没发现这个问题。

**修复:** 结果页优先用景点坐标生成高德 URI API 链接:

```
https://uri.amap.com/marker?position={longitude},{latitude}&name={name}
&coordinate=gaode&callnative=0&src=helloagents_trip_planner
```

坐标缺失时才退回“景点名 + 城市 + 官网 介绍”的搜索链接。`callnative=0`
保证在浏览器内打开,不强制唤起本地高德客户端。

**验证:** `npm run build` 通过;无头 Edge 从结果页读取生成链接并实际导航,
最终落到 `https://ditu.amap.com/regeo?...` 且页面包含“湖南省植物园”,
坐标保持 `113.02,28.11`。

### 2026-10-29(已完成:景点图片继续修正 + 景点信息入口)

> 本轮已完成实现、离线回归、真实长沙规划和结果页无头浏览器验证。

用户在湖南长沙结果页继续反馈:洋湖国家湿地公园、长沙湘江海洋王国仍显示示意图;
湖南省植物园命中的是入口停车场照片,代表性一般。用户还要求点击景点后进入官网
或相关信息。

**图片现状与已经完成的改进:**

1. Commons 不再取搜索第一条,已过滤图卷/绘画/地图/海报/低分辨率和异常长宽比;
2. 请求 10 个候选并把缩略图提升到 1600px,按标题、原图尺寸和横向构图评分;
3. 不再用城市风光冒充具体景点;
4. curl 单次失败不再立即熔断,服务端超时降级为无图片而不是 500;
5. 前端先渲染矢量示意图,远程图加载成功后才淡入,失败时不会空白;
6. 图片专项测试 6 项通过;前端 `npm run build` 通过。

**湖南样本实测:**

- 湖南省植物园:命中 Commons 的
  `Entrance of Hunan Forest Botanical Garden...`,4000x3000,但内容是入口停车场;
- 洋湖国家湿地公园:Commons 无精确结果,当前返回示意图;
- 长沙湘江海洋王国:Commons 无精确结果,当前返回示意图;
- 已额外尝试 Bing 图片、百度图片/百科、搜狗图片和去哪儿页面;百度/搜狗有反爬,
  Bing 中文结果相关性不稳定,暂不适合作为自动图片来源。

**景点信息入口(已完成):**

1. 后端 `_enrich()` 已把高德 `poi_id` 带入候选;
2. `_render_attractions()` 已把 `POI ID ...` 传给规划模型;
3. 规划提示词已要求模型把候选 POI ID 原样写入 `Attraction.poi_id`,禁止编造;
4. 新增 `_fill_attraction_poi_ids()`,模型产出后再按景点名与候选做确定性匹配,
   不完整依赖模型复制;
5. 前端 `Attraction` 类型已加入 `poi_id`;
6. 景点卡标题右侧已增加“查看信息 ↗”:有 POI ID 跳转
   `https://www.amap.com/place/{poi_id}`,没有则搜索
   “景点名 + 城市 + 官网 介绍”;
7. 新增 `tests/test_attraction_links.py`,覆盖确定性 POI ID 回填和禁止猜测;
8. 信息入口前端改动后 `npm run build` 已通过。

**最终验证:**

1. 景点链接、聊天、检索和图片四项专项测试共 **105 passed**;
2. 全量后端测试共收集 317 项,**316 passed / 1 skipped**。本机默认
   `%TEMP%\pytest-of-Admin1` 存在 ACL 拒绝访问问题,因此用
   `--basetemp=.pytest-tmp-attraction-links -p no:cacheprovider` 在工作区内运行;
3. `npm run build` 通过;
4. 后端已重启,真实长沙两日规划 `degraded=false`,56.3 秒完成,11 条 SSE
   事件分散在 40.2 秒内;
5. 高德文本检索确认湖南省植物园为 `B02DB027D2`、洋湖国家湿地公园为
   `B02DB0W7IB`,本次后端日志也完成了对应 POI 详情查询;
6. 无头 Edge 实际渲染结果页,两个景点分别显示“查看信息 ↗”,链接正确指向
   `https://www.amap.com/place/B02DB027D2` 和
   `https://www.amap.com/place/B02DB0W7IB`。

**已知限制:** Commons 没有这两个景点的可靠实景图时继续显示本地示意图;
“查看信息”入口不伪造图片或官网 URL。湘江海洋王国仍按同一规则处理。

**当前运行状态:**

- 后端 `0.0.0.0:8000`,PID `2312`(启动器 `11372`);
  MCP `0.0.0.0:8001`,PID `52732`;
- Vite `[::1]:5173`,PID `58700`;浏览器继续使用 `http://localhost:5173`。
- 后端日志:`%TEMP%\trip_planner_8000\out.log` / `err.log`;
- 前端日志:`%TEMP%\trip_planner_5173\out.log` / `err.log`。
- 当前后端已加载最新的 `poi_id` 检索、提示词和确定性回填改动。

### 2026-10-29(景点图片:高清实景筛选 + 无图兜底)

用户反馈图片有的不显示、有的不是景点真实图片、有的模糊。实测根因:

1. Commons 只取搜索第一条,故宫命中了“职贡图卷”绘画而不是景点照片;
2. 精确景点未命中时使用“城市 + 风景”兜底,会把同城图片冒充具体景点;
3. 缩略图只要 800px,前端卡片在高分屏或放大时容易模糊;
4. 单次 curl 偶发超时就会熔断整批图片,同批后续景点全部返回无图;
5. 前端在远程图加载前没有真实兜底,失败或网络慢时会短暂空白。

**修复:**

1. 删除城市风光替代,精确景点找不到时明确显示“景点示意图”;
2. Commons 搜索扩大到 10 个候选,请求 `dimensions`,缩略图提升到 1600px;
3. 过滤图卷、绘画、书法、地图、海报、平面图、低分辨率和异常长宽比;
4. 候选按标题相关性、原始宽高和横向构图评分,不再盲取第一条;
5. curl 增加重试,单次失败不再立即熔断;连续两次失败才短暂熔断;
6. `subprocess.TimeoutExpired` 等服务端超时统一降级为“无图片”,接口返回 200,
   不再把普通上游超时包装成 500;
7. 前端始终先渲染 800x500 矢量示意图,真实图片加载成功后才淡入;失败时保留
   示意图,并提高角标和票价标签的层级;
8. 前端图片元数据接口等待上限提升到 30 秒。

**真实接口抽查(北京):**

- 故宫博物院:2032x1524,命中 `北京故宫博物院.JPG`;
- 天坛公园:4349x2668;
- 毛主席纪念堂:2592x1944;
- 景山公园:4240x2832;
- 八达岭长城:3264x1836;
- 北海公园本轮上游超时,接口正常返回无图片,页面显示本地示意图。

**验证:** 图片专项测试 6 项通过;后端全量 `pytest` **313 passed / 1 skipped**;
前端 `npm run build` 通过。后端已重启,前端无需重启。

### 2026-10-29(聊天追问改为目的地 → 时间 → 注意事项三阶段)

用户实测“我想去北方玩”“逛古城吧”时,助手每轮都再次追问出发时间和天数。
根因不是模型偶发,而是提示词把城市、时间、天数当成同级必填项,并明确要求
缺日期时同时询问注意事项;推荐示例还写了“告诉我选哪个、大概什么时候去”。

**修复:**

1. 提示词改为严格三阶段:目的地未定时只确认城市;城市确定但时间不全时只问
   日期/天数;城市和时间齐全后才一次性确认同行人、饮食、行动便利性和节奏;
2. 新增 `enforce_clarification_stage()`,对模型越级生成的追问做确定性清洗,
   不依赖模型自觉遵守提示词;
3. `intent=clarify` 的回复先完成决策再分段发送。此前模型边生成边输出,即使
   后端随后发现“城市未定不该问日期”也无法撤回已经显示的文本;
4. 允许的澄清阶段不完整时,不再由 `enforce_optional_preference_check()` 提前
   追加注意事项;
5. 模型解析失败时的兜底话术也改为只问目的地,不再要求用户一次填完日期、
   天数、同行人和忌口;
6. 新增目的地缺失、城市已定但缺少时间、澄清文本流式清洗等回归用例;
7. 本地被忽略的 `frontend/.env` 仍保留旧 `http://localhost:8000`,已清空
   `VITE_API_BASE_URL` 并重启 Vite,改为走同源代理,避免浏览器直连 IPv6 失败。

**验证:** `pytest` **311 passed / 1 skipped**;前端无需改动,已保持运行。后端已
重启并完成 RAG 预热,8000 端口恢复监听。Vite 重启后页面、`/health` 代理和
`/api/auth/status` 均返回 200。

### 2026-10-29(README 对齐 + Docker/Compose + GitHub Actions)

补齐项目对外展示与可部署性,没有修改旅行检索、规划、记忆或鉴权业务逻辑。

**README:**

1. 删除“没有鉴权 / 没有多轮记忆 / 三次模型调用 / 三路检索”等过时描述;
2. 按当前实现更新四路并行检索、单次结构化生成、账号与配额、记忆、历史、
   RAG、SSE、降级和评测说明;
3. 增加本地启动、Docker 部署、数据持久化、公网安全边界和已知限制。

**部署配置:**

1. 新增 `backend/Dockerfile`、`frontend/Dockerfile` 和 `frontend/nginx.conf`;
2. 前端默认使用同源 API:开发时由 Vite 代理 `/api`、`/health`,生产时由
   Nginx 转发到后端;SSE 关闭代理缓冲并将读写超时提高到 300 秒;
3. 新增根目录 `docker-compose.yml`、`.env.compose.example` 和
   `DEPLOYMENT.md`;Compose 默认以 production 模式运行,并默认关闭匿名注册;
4. SQLite 与 FAISS 索引使用 `trip_data` 卷,HuggingFace/embedding 缓存使用
   `rag_models` 卷;后端端口默认只绑定宿主机 `127.0.0.1`;
5. 新增 `.github/workflows/ci.yml`,在 push / PR 时运行后端测试、前端构建和
   Compose 配置校验。

**验证:**

1. 后端全量离线测试通过,记录为 **309 passed / 1 skipped**;
2. `npm run build` 通过(3499 modules,Vite 提示主 chunk 约 2.2MB,属现有前端
   体积优化事项,不影响构建);
3. `docker-compose config --quiet` 和 Python YAML 解析通过;
4. 本机 Docker daemon 未启动,因此本轮**没有实际 `docker build` 或容器启动验证**。

**下一步:** 回到运行可靠性主线,优先做 Request ID + 结构化日志,再做结构化
日志可观测的全局规划并发背压。

### 2026-10-27(景点图片:Wikimedia 真实图片 + 本地即时兜底)

用户反馈结果页景点图片不显示。后端日志显示 `/api/poi/photo` 访问
`api.unsplash.com` 连续 10 秒超时,随后又用纯景点名重试一次;接口虽然返回
200,但 `photo_url=null`。前端还会等待全部图片请求结束后才初始化地图。

**修复:**

1. 新增 `app/services/wikimedia_photo_service.py`,从 Wikimedia Commons 搜索
   位图,拒绝 PDF 等非图片 MIME,并缓存成功/无匹配结果;
2. 网络失败后熔断 60 秒,图片请求最多同时执行 3 个,不再让每张图重复等待
   Unsplash 的两轮超时;
3. 前端立即渲染本地 SVG 景点示意图,地图和正文不再等待图片;
4. 图片请求使用可配置 API 地址、去重、AbortController 和 25 秒上限;真实图片
   成功后替换示意图,失败时保留示意图而不是显示空白或“加载失败”;
5. Wikimedia 图片保留来源链接,页面标注“图源 Wikimedia Commons”;无匹配时
   明确显示“景点示意图”。
6. 景点没有精确条目时,再用“城市 + 风景”查询同城 Commons 图片,并标注为
   “城市风光”;同一城市结果单独缓存;
7. 当前机器的 Python/OpenSSL 访问 Commons 会间歇性握手超时,服务优先调用系统
   `curl`(Windows Schannel),其它平台仍回退 `requests`。

**验证:** 郴州三处景点均返回真实图片:北湖公园命中精确图;五岭阁、南塔公园
命中郴州城市风光图。首次查询约 7~15 秒,已有缓存大多为 0~2 秒。全量测试
**309 passed / 1 skipped**;`npm run build` 通过。

### 2026-10-27(省份+城市名规范化:修复跨城 POI)

用户实测以“湖南郴州”发起规划时,页面标题是郴州,但候选景点在跨城检索中
返回了北京景点,酒店分布到郑州、深圳等地,地图也为空。

**根因:**

1. 高德 MCP 的 `city=湖南郴州` 不可识别;`citylimit=true` 实际失效,
   POI 搜索退化成全国范围结果,天气也返回 0 天;
2. 同一 MCP 实测 `city=郴州` / `郴州市` 均正常:天气 4 天,景点和酒店都在郴州;
3. 规划模型没有编造行程,而是如实指出了目的地与候选地点互相矛盾。

**修复:**

1. 新增 `app/utils/city_normalization.py`,统一处理省份+城市、自治区、直辖市、
   特别行政区、常见分隔符和后缀;例如“湖南郴州”“湖南省郴州市”都规范为“郴州”;
2. `TripRequest`、`POISearchRequest`、`RouteRequest` 和聊天 `ChatDecision`
   在入口自动规范化;高德服务层再做一次防御性规范化;
3. 保留省市同名/民族自治州等歧义场景,例如单独“吉林”和“海南藏族自治州”
   不会被错误截断。

**验证:**

1. 新增 `tests/test_city_normalization.py`,覆盖 29 个典型输入和 Pydantic 集成;
2. 真实三路检索故意输入“湖南郴州”,实际查询“郴州”:景点 6 个、天气 4 天、
   酒店 6 个,全部是本地候选,0 次模型调用;
3. 全量测试 **305 passed / 1 skipped**(共 306 项)。

### 2026-10-27(RAG 离线检索评测 + 无答案拒答)

补齐公共旅行知识 RAG 的可量化质量基线,没有调用规划模型。

**评测集与工具:**

1. 新增 `knowledge/eval_questions.jsonl`,共 38 题:22 个有答案、16 个无答案;
   覆盖精确事实、口语同义、时间敏感、实时边界、普通无答案和跨城市冲突;
2. 新增 `evals/rag_evaluator.py`、`scripts/eval_rag.py`,输出 `Hit@5`、
   `MRR@5`、无答案误命中率、城市串库率、P50/P95 和逐题候选;
3. 评测会校验预期 `doc_id` 存在且城市一致,避免标签写错;
4. 报告写入 `evals/results/rag/latest.json` / `latest.md`,该目录已忽略。

**检索修正:**

1. 原 BM25 使用空格分词,对中文实际只把整句当一个 token;改为无额外依赖的
   中文字符二元组 + 英文数字 token;
2. `KnowledgeChunk` 增加 `vector_relevance` / `bm25_score`,来源证据仍只暴露
   服务层生成的真实引用;
3. 原实现只要同城有文档就总会返回 candidate。新增双门槛:
   BGE 语义分 `>=0.62` 直接接纳;语义分 `>=0.55` 时还需中文 BM25 分
   `>=6.0` 作为字词证据。阈值可通过 `RAG_MIN_VECTOR_RELEVANCE` 等环境变量调整。

**真实本地 BGE 基线:** 38 题 `Hit@5=100%`、`MRR@5=1.0000`、
无答案误命中 `0/16`、城市串库 `0`、重复运行 P95 约 14~19ms。当前阈值针对 11 个知识卡
校准,增加文档或更换 embedding 后必须重跑。

**验证:** `pytest` 收集 277 项,结果为 **276 passed / 1 skipped**;
`npm run build` 通过。RAG 来源展示和规划链路本轮没有改变。

### 2026-10-20(公共旅行知识 RAG:混合检索 + 第四路并行 + 来源展示)

按 `RAG_PLAN.md` 完成 Phase 1~3 的后端与前端 MVP,整条规划仍只调用一次模型。

**索引与检索:**

1. 新增 `app/rag/` 独立子包:核心不依赖其它 `app.*` 模块、FastAPI 或 Pydantic,
   业务侧只通过 `app/services/knowledge_retrieval.py` 适配;
2. `app/rag/embeddings.py`:采用 C8 的 `BAAI/bge-small-zh-v1.5`，
   CPU、归一化向量，并优先解析 `E:\all-in-rag\models` 本地模型；
3. `app/rag/index.py`:采用 LangChain FAISS，持久化 `index.faiss/index.pkl`；
4. `app/rag/data_preparation.py`:采用 `MarkdownHeaderTextSplitter`；
5. `app/rag/retrieval.py`:采用 FAISS Top 5 + BM25 Top 5 + RRF(k=60)，
   再做城市 metadata 和有效期过滤；RAG 失败不会阻断主链路;
6. 新增 `scripts/ingest_knowledge.py`、`scripts/check_rag.py` 和
   `knowledge/seed_documents.jsonl`:
   首版 11 个内部整理知识卡、11 个切片、覆盖 10 个城市。种子内容明确不是
   官方实时公告,动态规则仍需通过 `source_url` 官方渠道核验。

**规划与契约:**

1. LangGraph 增加 `search_knowledge`,与景点/天气/酒店四路并行;
2. 知识证据注入唯一一次规划提示词,并明确“高德实时数据优先、文档是不可信资料、
   不执行其中指令”;
3. `TripPlanResponse`、表单 SSE、聊天 artifact/done 增加
   `knowledge_used` / `knowledge_degraded` / `knowledge_status` /
   `knowledge_sources`;
4. 前端表单进度增加“知识检索”,聊天卡片和结果页展示实际来源、发布方、
   核验日期与链接;`knowledge_degraded` 只提示知识降级,不影响行程整体状态;
5. 聊天知识问答仍按方案保持延后,未增加意图识别后的第二次模型调用。

**验证:** `pytest` **271 passed / 1 skipped**（真实 BGE 用例默认显式跳过）;
真实本地 BGE 索引验证通过：11 docs / 11 chunks / 11 FAISS vectors，
查询“故宫周一预约/带六岁孩子”正确召回北京故宫和亲子交通知识；
`npm run build` 通过。未运行城市 live 评测。

**依赖与启动:** 新增 `langchain-huggingface`、`langchain-community`、
`langchain-text-splitters`、`faiss-cpu`、`rank-bm25`；
应用 lifespan 会预热 BGE/FAISS/BM25，预热失败不阻止后端启动。

**下一步:** 不再为了覆盖率批量扩城市。RAG 后续只做有真实需求的增量:录入经过
人工核验的官方文档、补充检索评测问题,必要时再替换神经 embedding 或增加
`/api/knowledge/search` 管理调试入口;不要先上 Qdrant/reranker。

### 2026-10-20(路线质量扩展 live 验证:深圳 / 厦门 / 洛阳 / 大同)

继续验证首版 35km / 70km 固定距离约束在亲子、步行和二线城市上的泛化能力。
本轮没有改运行时代码,使用全新一次性账号、每个 case 前清空长期偏好,真实调用
`POST /api/trip/plan/stream`。

**live 结果:**

1. `shenzhen-family-3d`:47.7 秒,100 分;非必去远景点 0,
   三日路线跨度 15.7 / 15.0 / 0.0km(上限 70km);
2. `xiamen-relaxed-2d`:49.0 秒,100 分;25km 外非必去景点 0,
   两日路线跨度均为 0.2km(上限 45km);
3. `luoyang-longmen-3d`:45.4 秒,100 分;龙门石窟必去约束通过;
4. `datong-yungang-3d`:57.0 秒,100 分;云冈石窟必去约束通过。

汇总 **4/4 通过、平均分 100、平均 49.77 秒、降级 0**。结合此前批次,
目前已有 8 个不同城市场景通过 live 基线。期间 MCP 日志仍出现已知的 Windows
`_ProactorBasePipeTransport` connection reset 回调,但四份计划均成功,没有降级。

报告位于 `evals/results/latest.json` / `latest.md`。由于本轮没有修改运行时代码,
没有重复运行离线测试。

**范围边界:** 不再按城市穷举 golden cases。当前 8 个 live 场景已经覆盖经典历史、
亲子休闲、步行紧凑、饮食偏好和远距离必去,足以判断首版固定阈值是否可用。
剩余 case 只在三种情况下运行:用户明确要求;出现线上失败需要回归;功能变更
影响了检索、排序或必去约束。固定阈值暂不需要改成城市分位数或聚类。

### 2026-10-20(行程质量约束:距离排序 + 紧凑路线评测)

完成此前识别出的下一个主线模块。问题不是 schema 或日期错误,而是候选景点虽然
结构合法,却可能被排到几十公里外的远郊。本轮仍保持整条规划链路只调用一次模型。

**确定性景点质量:**

1. 新增 `app/services/itinerary_quality.py`:统一坐标解析、Haversine 距离、
   紧凑偏好识别、候选排序、紧凑候选保留和单日路线跨度计算。
2. 检索详情回填后,候选会带“距城市中心 X 公里”。亲子、小孩、老人、少走路、
   无障碍、休闲、慢逛等请求会优先选择市内候选,并降低山地、长城、徒步、漂流等
   高体力名称的排序。
3. 紧凑模式在本地候选足够时直接剔除 35 公里外非必去项;只有候选不足时才补入
   最近远端项。用户点名的必去景点始终置顶、不会被过滤。
4. 规划提示词会动态增加路线舒适度约束,要求同日同区、少折返,并明确输入中的
   距离字段。普通历史文化等请求仍保持原有景点相关性排序。

**顺带修复真实数据问题:**

1. 高德 MCP 的 `maps_geo` 实际返回 `{"return": [...]}`,旧解析只认
   `geocodes`,导致城市中心始终为 `None`、距离排序没有生效。现在兼容
   `return` / `geocodes` / `data` 和顶层列表。
2. 评测器把“秦始皇帝陵博物院”和“兵马俑”识别为同一地点的别名,
   不再要求模型名称里同时包含两个字符串。

**质量评测:**

1. `route_quality` 支持城市中心坐标、单日远景点上限和单日路线跨度上限;
   用户明确点名的远景点不计入扣分。
2. 新增 `beijing-family-3d` 回归 case,并给上海亲子、深圳亲子、厦门休闲 case
   增加路线约束。
3. `scripts/eval_trip_plans.py` 在 live 模式下默认每个 case 前清空专用账号的
   长期偏好,避免上一组“亲子 / 少走路”污染下一组;需要保留时传
   `--keep-evaluation-memory`。

**真实 live 验证(同一版代码、全新隔离账号):**

1. `beijing-family-3d`: 58.0 秒,100 分,非必去远景点 0;
2. `chengdu-food-4d`: 69.8 秒,100 分;
3. `xian-terracotta-3d`: 61.3 秒,100 分,兵马俑硬约束通过;
4. `suzhou-gardens-2d`: 43.4 秒,100 分。

汇总 **4/4 通过、平均分 100、平均 58.12 秒、降级 0**。报告位于
`evals/results/latest.json` / `latest.md`。

**离线验证:** `pytest` 242 → **258 全绿**。

**下一步:** 当前 35km / 70km 阈值是面向家庭休闲请求的首版保守值。下一轮应扩大到
深圳亲子、厦门步行和更多二线城市,观察 MCP 候选质量;如果本地候选经常不足,
再考虑用“城市尺度分位数 + 景点聚类”替代固定距离,而不是放松用户约束。

### 2026-10-20(聊天追问措辞多样化 + 已问过禁止复述)

用户反馈截图里的助手虽然已经补上了注意事项,但结尾总是同一句:
"另外有没有同行老人或小孩、饮食忌口、行动便利性、节奏偏好或必去地点需要注意?"
要求语义保持不变,但表达应随机自然,不能成为固定模板。

**根因:**

1. 提示词里写了一个近乎完整的固定例句,模型直接把它当答案模板;
2. 历史里虽然已有上一轮追问,但旧规则主要限制"用户补充日期后"不再问,
   用户在中间插入"郴州有什么好玩"时仍可能再次复述。

**本轮完成:**

1. 删除固定追问例句,改为语义检查清单 + 每轮随机注入一种表达风格
   (口语、简短、温和、活泼、先回应再追问等),要求模型不要照抄固定句式。
2. `enforce_optional_preference_check()` 的确定性兜底也改为从 6 条不同问法里随机选择,
   不再使用固定句子。
3. `_format_context()` 新增 `【注意事项确认状态】`。只要历史中助手已经问过,
   后续明确标记"绝对不要再次询问",用户聊景点或补日期时都不得复述。
4. 扩充识别模式,覆盖"同行老人 / 饮食限制 / 步行强度 / 无障碍"等同义表达,
   避免模型换个说法后系统误判为没问过。
5. `scripts/check_chat.py` 现在会把第一轮助手回复写回历史,并支持通过
   `TRIP_CHAT_FIRST_MESSAGE / TRIP_CHAT_SECOND_MESSAGE` 验证任意两轮对话。

**真实验证(湖南郴州):**

1. "我想去湖南郴州" → 助手用不同口语问法询问日期、人数、饮食和同行情况;
2. "湖南郴州有什么好玩的?下周末出发,玩三天" → 回答景点后直接规划,
   **没有再次复述注意事项**;
3. 输出郴州 3 天行程,`degraded=false`,约 60.5 秒。

**验证:** `pytest` 238 → **242 全绿**;后端已重启加载最新逻辑。

### 2026-10-20(聊天追问:补上同行人 / 饮食忌口等注意事项)

用户反馈:选定城市或选择推荐城市后,助手永远只问"什么时候出发、玩几天",
从不问带小孩、饮食忌口、行动便利性或节奏。

**根因:** 旧提示词把"城市 + 时间"定义为唯一放行条件;日期缺失时模型自然
只会追问日期。`preferences` 虽然是槽位,但没有任何"首次规划前确认一次"的规则。

**本轮完成:**

1. 提示词新增「一次性注意事项确认」:目的地明确后,日期/天数缺失时应同时问
   同行老人或小孩、饮食忌口、行动便利性、节奏偏好和必去地点;只问一次。
2. 新增确定性兜底 `enforce_optional_preference_check()`:
   - 模型漏问时自动补问;
   - 用户已给偏好、明确说"没有特别要求/按默认",或历史里已问过时不再重复;
   - 城市都没确定时不提前问饮食和同行人。
3. 新增 `promote_complete_clarification()`:模型把可推导信息误报为缺失时纠正。
   例如用户已给"下周末 + 三天 + 带小孩",模型仍问返程日期,后端会按
   `start_date + travel_days` 直接组装行程并继续规划,不再卡在追问。
4. 提示词明确:出发时间 + 时长已足够;返程日可由后端推导;交通/住宿有系统默认值,
   都不能作为继续追问的理由。
5. 模型常把未指定槽位写成字符串 `"None"` / `"null"` / `"无"`。`ChatDecision`
   现在统一清洗,交通/住宿回到默认值,不会把字面 `"None"` 带进规划。

**真实验证:** 用专用账号跑两轮聊天:

1. "我想去北京玩三天" → 同时询问出发日期、同行老人/小孩、饮食忌口、
   行动便利性、节奏和必去地点;
2. "下周末出发,带一个六岁的小孩" → 解析为北京 3 天、10-24 至 10-26,
   `degraded=false`,约 55 秒拿到行程 artifact。

**验证结果:** `pytest` 226 → **238 全绿**;后端已重启加载新逻辑。

**仍待优化(不是本轮追问 bug):** 这轮真实北京亲子行程选中了金海湖、百里山水画廊、
千灵山等远离市区的景点,结构与约束都合法,但"亲子友好 / 市内交通合理性"仍偏弱。
下一轮应根据 live 失败样本加强检索后的距离与适龄约束。

### 2026-10-19(旅行规划质量评测中心)

建立主线质量基线工具,不进入线上请求链路,也不新增依赖。

**数据与评分:**

1. `evals/golden_cases.json` 内置 18 个 case,覆盖经典城市、亲子、美食、
   自然风光、文化城市、2/3/4 天行程和多个必去景点场景。
2. `evals/evaluator.py` 检查响应成功、非降级、`TripPlan` schema、城市、
   日期范围、天数、每日日期序列、每日景点数量、唯一景点数量、必去地点、
   坐标、占位内容、总体建议、预算分项合计和端到端耗时。
3. 每个 case 输出 0~100 分、通过状态和具体失败检查;报告汇总通过率、
   平均分、降级数和平均耗时。

**运行方式:**

1. `scripts/eval_trip_plans.py` 支持 `--list-cases`、`replay` 和 `live`。
2. `replay` 只读取已有响应,不花额度;样例位于
   `evals/fixtures/sample_replay.json`,明确标注为冒烟数据而非质量基线。
3. `live` 每个 case 会真实调用一次 `/api/trip/plan/stream`,必须同时传
   `--yes-live`;建议先 `--limit 3`,并使用无历史偏好的专用评测账号。
4. 报告写入已忽略版本库的 `evals/results/latest.json` / `latest.md`;
   `--fail-under` 可用于 CI 通过率门禁。

**验证:** 新增 `tests/test_plan_evaluation.py`,覆盖案例完整性、必去地点缺失、
日期错位、降级占位识别和报告汇总。`pytest` 221 → **226 全绿**;
离线样例报告为 2/3 通过(第 3 个是故意模拟的降级失败)。本轮未调用模型。

**首轮 live baseline:** 使用专用临时评测账号跑了前 3 个 case,结果 **3/3 通过**、
平均分 100、平均耗时 **50.83 秒**、降级 0:

- `beijing-classic-3d`: 49.6 秒;
- `beijing-must-visit`: 51.2 秒,故宫硬约束通过;
- `shanghai-family-3d`: 51.7 秒。

报告位于 `evals/results/latest.json` / `latest.md`。运行期间 MCP 日志出现过已知的
Windows `_ProactorBasePipeTransport` connection reset 回调告警,但三份计划均成功,
检索没有降级;这与第 2.5 / 第四轮记录过的 MCP 会话重建现象一致。

**下一步:** 不要把 3/3 当成完整质量结论。下一组优先跑高差异案例:
`chengdu-food-4d`(不吃辣)、`xian-terracotta-3d`(必去兵马俑)、
`suzhou-gardens-2d`(两天短行程),再决定是否扩到完整 18 个。

### 2026-10-19(用户可见的历史行程)

把已经写入 `trip_history` 的成功行程变成真正可浏览的产品功能。没有新增
数据表、迁移、依赖或模型调用。

**后端:**

1. 新增 `GET /api/history`:按当前 Bearer token 的用户身份分页读取成功行程,
   支持城市精确筛选,返回城市、日期、天数、偏好、建议摘要和主要景点。
2. 新增 `GET /api/history/{history_id}`:返回当时的请求和完整 `TripPlan`。
3. 新增 `DELETE /api/history/{history_id}`:只删除当前用户自己的单条历史记录。
4. `MemoryStore` 增加 `offset` 分页、总数统计、单条读取和单条删除;
   跨用户读取或删除统一表现为 404,不泄露记录是否存在。

**前端:**

1. 顶栏新增「历史行程」入口和 `/history` 路由。
2. 历史页支持城市筛选、分页卡片、保存时间、偏好和主要景点摘要。
3. 点击卡片打开详情抽屉,复用 `TripPlanView` 渲染完整行程。
4. 支持单条删除;删除当前详情时会同步关闭抽屉并刷新总数。

**验证:** 新增 `tests/test_history_api.py`,覆盖登录要求、用户隔离、城市筛选、
详情、越权读取/删除和删除后的列表刷新。`pytest` 218 → **221 全绿**;
`npm run build` 通过。开发服务已重启并确认 `/api/history` 出现在 OpenAPI,
未带 token 返回 401;Vite `/history` 页面与组件模块均返回 200。

**下一步:** 仍按主线进入 golden cases 评测;历史功能保持"只读/删除",暂不扩成
第二套记忆或向量召回系统。

### 2026-10-19(主线可靠性:总超时覆盖惰性建图)

修复交接文档里记录的真实隐患:首次调用时 `_ensure_graph()` 位于总超时之外,
如果 MCP Server 启动或 LangGraph 初始化卡住,240 秒总闸不会触发。

**本轮完成:**

1. 非流式 `plan_trip_with_status()` 的 `asyncio.timeout` 现在同时覆盖
   `_ensure_graph()` 和 `graph.ainvoke()`。
2. 流式 `plan_trip_stream()` 同样把建图放进总闸;原来的三路 `running`
   进度事件仍在建图成功后发送,事件顺序未改变。
3. 新增两个离线用例分别覆盖流式和非流式的“首次建图永远不返回”,确认最终
   返回 `degraded=true`,原因含“规划超时”,而不是无限挂起。

**验证:** `pytest` 216 → **218 全绿**;未调用模型、未联网。

**下一步:** 回到 golden cases 评测集,先量化规划质量与端到端耗时。

### 2026-10-19(账号生命周期:改密 / 禁用 / 一次性找回 / 审计)

补齐自托管账号的后续管理能力,仍未引入角色系统、邮件服务或新依赖。
本轮不修改旅行检索、记忆、聊天或规划 Agent。

**账号能力:**

1. `/api/auth/change-password`:校验旧密码后改密,撤销该账号全部旧会话和
   待用重置令牌,直接返回新 Bearer token。
2. `/api/auth/admin/users/{user_id}/disable|enable`:管理员禁用会立即撤销全部
   会话和重置令牌;重新启用后可以正常登录。
3. `/api/auth/admin/users/{user_id}/password-reset-token`:管理员签发一次性
   找回令牌;数据库只存令牌 SHA-256,默认 15 分钟过期。
4. `/api/auth/password-reset/confirm`:消费一次性令牌设置新密码,并撤销全部旧
   会话;同一令牌不能再次使用。

**管理员鉴权:**

1. 新增 `AUTH_ADMIN_KEY`;通过 `X-Admin-Key` 请求头访问管理接口。
2. 密钥为空时管理接口返回 404;缺失或错误返回 403。
3. 生产环境若配置了管理密钥但短于 32 字符,启动配置校验直接报错。
   反向代理后面仍要先处理可信客户端 IP,不能直接信任任意 `X-Forwarded-For`。

**审计:**

1. `auth_audit_events` 记录注册、改密、禁用/启用、重置令牌签发和重置完成的
   成功/失败结果。
2. 审计只存用户 ID、规范化用户名、来源地址哈希和简短结果,不保存密码、
   Bearer token 或重置令牌明文。
3. `/api/auth/admin/audit` 支持按 `event_type` 过滤,最多返回 200 条。

**验证:** 新增 `tests/test_account_lifecycle.py`,覆盖旧会话撤销、重置令牌
单次使用、禁用后立即失效、启用后恢复登录、管理密钥保护、改密令牌轮换和审计。
`pytest` 211 → **216 全绿**;未调用模型、未联网。

**下一步:** 账号生命周期闭环到此为止,回到旅行规划主线。优先建立 10~20 个
golden cases,离线度量必去景点遵守率、日期/天数一致性、JSON 合法率、降级率和
真实端到端耗时,再决定缓存、RAG 或模型替换。

### 2026-10-19(登录失败限流 + 按用户模型额度)

补上账号鉴权之后最紧急的成本与爆破防护。仍然零新增依赖、零模型调用。

**登录失败限流:**

1. `auth_login_failures` 表记录“用户名 + 直接来源 IP”的失败事件;
   组合键只存 SHA-256,不重复存明文。
2. 默认滑动窗口 15 分钟、最多 5 次失败。达到上限后 `/api/auth/login`
   返回 `429` + `Retry-After`;成功后清除该组合的失败记录。
3. 失败名额在 PBKDF2 之前用 `BEGIN IMMEDIATE` 原子预占,并发请求也不能
   同时越过上限。不存在的用户名也走同成本密码哈希,避免用响应耗时枚举账号。
4. 新增 `AUTH_LOGIN_MAX_FAILURES` / `AUTH_LOGIN_FAILURE_WINDOW_SECONDS`。

**按用户模型额度:**

1. 新增 SQLite `model_usage_daily` 表;默认每天 30 个“模型调用单位”,
   按 UTC+8(北京时间)零点重置。
2. `/api/trip/plan`、`/api/trip/plan/stream` 各预占 1 单位;
   `/api/chat/stream` 的结构化直传预占 1 单位,自然语言按最多 2 单位
   (意图识别 + 可能的规划)预占。
3. 预占在昂贵链路开始前执行,并直接发生在返回 SSE 响应之前;额度不足统一
   返回 `429` + `Retry-After`,不会先建立流再失败。
4. 成功响应的 `X-Model-Quota-Limit / Used / Remaining / Reset-At`
   暴露给前端;这些响应头已加入 CORS `expose_headers`。
5. 配置项:`MODEL_QUOTA_ENABLED`、`MODEL_QUOTA_DB_PATH`、
   `MODEL_QUOTA_DAILY_CALL_LIMIT`、`MODEL_QUOTA_UTC_OFFSET_HOURS`。

**部署注意:** 登录限流当前取 `request.client.host`,即反向代理的直连地址。
上生产前如果后端位于 Nginx / 云负载均衡之后,必须先确定可信代理边界,再接入
真实客户端 IP;不能直接信任任意客户端传来的 `X-Forwarded-For`。

**验证:** 新增 `tests/test_access_guard.py`,覆盖滑动窗口、成功清零、
用户名 + IP 隔离、北京时间跨日重置、20 个并发预占不超卖,以及三个模型入口的
429 契约。`pytest` 201 → **211 全绿**。

**当时下一步(现已完成):** 账号禁用 / 改密 / 找回、注册审计;
之后再做真实 token 用量统计
(当前额度是请求前的调用单位预占,不是账单级 token 计量)。

### 2026-10-19(真实鉴权:账号 + Bearer token + 前端登录)

补上真实账号鉴权,不再信任客户端随便传的 `user_id`。没有新增依赖。

**后端完成:**

1. 新增 `app/services/auth_service.py`:
   - 用户名密码账号存 SQLite `data/trip_auth.sqlite3`;
   - 密码使用 PBKDF2-HMAC-SHA256(随机盐、310k 次迭代);
   - 登录签发随机 opaque token,数据库只存 token 的 SHA-256;
   - token 默认 30 天过期,退出登录立即撤销。
2. 新增 `/api/auth/status|register|login|me|logout`。
3. `AUTH_ENABLED` 默认 `true`;行程、聊天、记忆接口都必须带
   `Authorization: Bearer <token>`。
4. `TripRequest.user_id` / `ChatRequest.user_id` 只保留兼容,鉴权开启时会
   被 token 对应用户覆盖;`/api/memory/{user_id}` 访问他人 ID 返回 403。
5. 注册可通过 `AUTH_ALLOW_REGISTRATION` 关闭;非空
   `AUTH_REGISTRATION_KEY` 时注册必须提供邀请码。生产环境如果开放注册却没设
   邀请码,启动配置校验会报错。
6. `tests/conftest.py` 默认为旧功能测试关闭鉴权;`tests/test_auth.py` 显式开启
   鉴权,覆盖密码校验、令牌撤销、401、越权 403 和客户端 `user_id` 覆盖。

**前端完成:**

1. 新增 `/login` 登录 / 注册页,支持邀请码。
2. 路由守卫要求先登录;Axios 和 fetch SSE 都自动注入 Bearer token。
3. 收到 401 自动清除本地登录态并跳转登录页。
4. 顶栏显示当前用户名并提供退出登录。
5. `session.ts` 不再生成本地随机 `user_id`;会话 ID 保留,用户身份来自 token。
   会话 ID 按登录用户分区,且登录态变化会清空 `sessionStorage` 中的上一份
   行程与记忆回执,避免同一浏览器换账号时串数据。

**验证脚本:** `check_stream.py` / `check_chat.py` / `check_chat_ui.py`
支持 `TRIP_AUTH_TOKEN`,或 `TRIP_AUTH_USERNAME` + `TRIP_AUTH_PASSWORD` 自动登录。

**验证:** `pytest` 193 → **201 全绿**;`npm run build` 通过。

**当时仍待补(现已完成):** 账号禁用/改密/找回和注册审计;登录失败限流与按用户模型配额
已在后续轮次完成。

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

> **2026-10-19 已修复:** 流式和非流式的总超时都已覆盖 `_ensure_graph()`。
> 底层同步调用若完全阻塞事件循环仍无法由 asyncio 打断,但本次已消除
> “首次异步初始化无限等待却不受总闸约束”的明确缺口。

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

### P0 · 记忆与鉴权(基础闭环已完成)

> **2026-10-27 RAG 质量基线**:38 题离线评测已达到 `Hit@5=100%`、
> `MRR@5=1.0000`、无答案误命中 `0/16`、城市串库 `0`、P95 约 14~19ms。
> 中文 BM25 已改字符二元组,并加入 BGE + BM25 双门槛。阈值是当前 11 个知识卡
> 的校准值;**扩资料或换 embedding 后必须先重跑 `scripts/eval_rag.py`**。
> **2026-10-29 更新:**README、Docker/Compose、部署说明和 GitHub Actions CI
> 已完成;聊天也改为“目的地 → 时间 → 注意事项”三阶段追问。下一步不继续扩
> 城市或知识卡,优先补 Request ID + 结构化日志,再做全局规划并发背压;
> 缓存、Qdrant、reranker 和聊天知识问答继续延后。

> **2026-10-20 RAG 进展**：公共旅行知识 RAG 已按 C8 重做。
> 核心位于 `app/rag/` 独立子包：`BAAI/bge-small-zh-v1.5` + FAISS +
> BM25 Top 5 + RRF(k=60)、Markdown 标题切片、城市/有效期过滤、
> LangGraph 第四路并行检索、响应来源字段和前端来源展示均已落地;
> 规划仍只有一次模型调用。首版种子库为 11 个内部知识卡,不是官方实时公告。
> `pytest` **271 passed / 1 staged skip**,真实本地 BGE 索引验证通过,
> `npm run build` 通过。聊天知识问答、Qdrant 和 reranker
> 仍明确延后;后续只按真实需求补充人工核验资料,不按城市批量扩张。

> **2026-10-20 追加进度与范围纠偏**:首版固定距离阈值已在深圳亲子、厦门步行、
> 洛阳龙门石窟和大同云冈石窟四个 live case 上全部通过,最新批次 **4/4、
> 平均分 100、平均 49.77 秒、降级 0**;累计已有 8 个不同城市场景通过。
> **不再按全国城市穷举评测**;剩余 case 只在用户要求、线上失败回归或相关功能
> 变更时运行。当前固定阈值保持现状,不改造成分位数 / 聚类方案。

> **2026-10-20 进度**:**行程质量约束模块已完成**。景点候选现在按城市中心距离和
> 紧凑偏好确定性排序,家庭 / 休闲请求在候选充足时会过滤 35 公里外非必去项;
> golden case 新增北京亲子回归及 `route_quality`。全新隔离账号的 4 个高差异
> live case 为 **4/4 通过、平均分 100、平均 58.12 秒、降级 0**。随后新增的
> 深圳、厦门、洛阳、大同验证也全部通过;质量范围验证到此收敛,**不再按城市
> 穷举**。只有出现失败样本或相关功能变更时才补回归,不为了覆盖率盲目烧额度。

> **2026-10-19 进度**:第 1 步“服务端会话 + refine”已完成并提交
> (`71a0bc1`);`memory_facts` 的事实生命周期与
> `GET/DELETE /api/memory/{user_id}`、显式偏好抽取、规划前全量注入和
> `used_memory` 回执均已完成;`trip_history` 的结构化情景记忆也已完成,
> 支持“照上次北京那次”并返回 `used_history`。工作 / 长期偏好 / 情景三层闭环
> 均已完成,前端聊天 / 结果页也已展示记忆回执,**仍然不做向量库**。
> 账号密码、Bearer token、前端登录与接口身份绑定也已完成。
> 登录失败滑动窗口限流和按用户模型日额度也已完成。
> 账号生命周期管理(禁用 / 改密 / 一次性找回 / 审计)也已完成。
> golden cases 评测中心已建立,首轮 3 个 live case 为 3/3 通过、平均 50.83 秒。
> 下一阶段先跑高差异案例(成都饮食、西安必去、苏州短行程),扩大基线覆盖后再
> 决定做缓存、RAG 还是模型/提示词优化;真实 token 用量统计继续作为工程化候选项。

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

> RAG 知识库已完成 MVP 与检索评测,现状见下一节。

### P0.5 · 公共旅行知识 RAG —— MVP 与检索评测已完成

> **2026-10-27 状态**:Phase 1~3、38 题离线检索评测和无答案拒答均已完成。
> 后续只按真实业务需求补充人工核验资料,并在资料变化后重跑评测。

当前完成情况:

1. 首版 11 个知识卡、10 个城市和 38 个评测问题已落地;
2. BGE + FAISS + 中文 BM25 + RRF 混合检索已落地,暂不引入独立向量库;
3. LangGraph 第四路 `search_knowledge` 已与实时检索并行,规划仍只调用一次模型;
4. 响应只使用服务层生成的 `knowledge_sources`;
5. RAG 无命中、超时或索引失败只标记 `knowledge_degraded`;
6. 聊天知识问答、reranker 和 Qdrant/pgvector 继续延后。

**明确边界:** 高德仍是坐标、天气、酒店等动态事实的权威来源;用户记忆继续使用
SQLite 结构化存储,不写入公共知识向量索引。

### P1 · 结果级缓存 —— 已按要求标记延后

同一 (city, preferences, 日期, 住宿) 直接复用上次的 `TripPlan`(带 TTL)。
这是唯一能把**重复请求**压进秒级的手段 —— 但 2026-10-16 用户明确说
"留着以后优化的时候做"。记忆系统现已完成;缓存仍需先定义天气、闭馆和价格等
动态信息的 TTL 与失效规则,当前不启动。

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
- **全局并发背压**:登录限流与用户日配额已完成;若公开部署,还需要限制
  同时在跑的 `/plan`,避免每个请求内部的三路检索叠加压垮上游。
- **成本护栏**:按调用单位的单用户日配额已完成;后续升级为真实 token 统计。
- **接口鉴权与配额**:已完成账号鉴权、登录限流和模型调用单位配额。
- **可观测性**:接入 LangSmith / Langfuse,追踪工具调用、token、耗时。
- **评测集**:城市 × 偏好的 golden cases,度量 JSON 合法率、端到端时延、兜底率。
- **Docker 化**:注意镜像里得装上 `uvx`(高德 MCP 靠它拉起)。
- **密钥轮换**:`.env` 从未进过版本库,但 ModelScope / 高德 / Unsplash 的 key
  建议手动轮换一次 —— 这条原本只写在被删掉的那两份文档里,别跟着一起丢了。
- **清理配置过渡残留**:`config.py` 仍加载 `HelloAgents/.env`、保留 `openai_*` 字段。

### 明确不要做

- **无依据扩量 RAG 知识库**:当前 11 个知识卡和 38 题评测已形成基线;不为
  覆盖率批量添加未核验资料,不先上独立向量库或 reranker。
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
