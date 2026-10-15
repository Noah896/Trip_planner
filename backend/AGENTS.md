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
            └─ generate_plan       → response_format=TripPlan 结构化输出
                 失败 → planner_fallback + extract_json
       任何一步失败 → _degrade() 返回占位行程,degraded=true(不假装成功)
```

| 关注点 | 文件 |
| --- | --- |
| LangGraph 编排 / 4 个 Agent / 兜底 | `app/agents/trip_planner_agent.py` |
| 高德 MCP 连接与常驻 Server 生命周期 | `app/services/amap_mcp.py` |
| LLM 客户端(含 thinking 开关) | `app/services/llm_service.py` |
| 配置与凭据解析 | `app/config.py` |
| 高德结果解析 | `app/services/amap_service.py` |
| 对外契约(请求/响应模型) | `app/models/schemas.py` |
| 应用生命周期 / 路由注册 | `app/api/main.py` |

---

## 4. 进度日志

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

测试:50 → **61 全绿**(新增 `tests/test_amap_mcp.py`)。改动**尚未提交**。

### 仍未解决

- ❌ **端到端仍未跑出 `degraded=false`**。上述两个 bug 修掉后,`/plan` 依然
  11 分钟以上不返回(客户端超时)。最新判断:
  **瓶颈已从 MCP 转移到「模型侧延迟 / Agent 循环」**,详见第 5 节。

---

## 5. 下一步

### P0 · 把单次请求压到可用区间(当前最大的未解问题)

1. **先做隔离诊断**(只花几次模型调用,比反复跑整条链路划算):
   单独计一个「检索 Agent(挂 MCP 工具)」和「规划 Agent」,
   打印**耗时 / 消息序列 / 工具调用次数 / 输出长度**。
   脚本骨架:`ensure_amap_server()` → `get_amap_tools()` →
   `create_agent(model=get_llm(), tools=..., system_prompt=ATTRACTION_AGENT_PROMPT)`
   → `ainvoke` 计时。
   > 上一次跑到一半被用户中断,结论未取到。
2. 根据诊断结果处理,大概率是下面两条(可同时做):
   - **收敛检索 Agent**:`ainvoke(..., config={"recursion_limit": N})`;
     并收紧提示词 —— 现在 `ATTRACTION_AGENT_PROMPT` 里那句
     「如果一次结果太少,可以换关键词再检索一次」在鼓励模型反复检索。
   - **给模型输出加上界**:`ChatOpenAI(max_tokens=...)`。
   - 注意:`LLM_TIMEOUT=60` 对**流式**响应是「两次读取之间的间隔」超时,
     **不会**截断总时长,所以它现在基本形同虚设,
     必须另外加总时长 / 输出长度约束。

### P1 · 坐标回填(已确认的 bug,修法已验证)

- 让检索层拿到 `poi_id` 后调 `maps_search_detail` 取真实坐标,
  由**服务层回填**,而不是让模型猜。
- 落点:`app/agents/trip_planner_agent.py` 的检索节点 + `app/services/amap_service.py`。
- 顺带可以把 `rating` / `level` / `opentime` 也回填进去,前端信息更实。

### P2 · 工程化(见 `NEXT_STEPS_CHECKLIST.md`)

结构化日志(现在还是 `print` + emoji,`loguru` 已在依赖里)、
真实依赖健康检查(现在只查 key 存不存在)、
限流 + 并发背压、结果级缓存、Docker 化。

### 明确不要做

**在单轮请求都还没跑通之前,不要动记忆 / RAG。**

---

## 6. 本次未提交的改动

```
 M README.md                            # 端口说明 + 已知限制(原「每次调用起进程」已修)
 M backend/.env.example                 # MCP 常驻配置 + LLM_ENABLE_THINKING 说明
 M backend/app/api/main.py              # 关闭时回收常驻 MCP Server
 M backend/app/config.py                # 新增 llm_enable_thinking
 M backend/app/services/amap_mcp.py     # pydantic 约束 + root_cause + streamable-http 常驻
 M backend/app/services/llm_service.py  # extra_body enable_thinking
?? backend/tests/test_amap_mcp.py       # 新增用例覆盖上述改动
```

改动都用 `git diff` 逐行核对过。跑完测试再提交。
