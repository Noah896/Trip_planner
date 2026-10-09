# trip_planner · 下一步清单

> 背景:后端已完成 HelloAgents → LangChain + LangGraph 重构(见 `REFACTOR_LANGCHAIN.md`)。
> 关键判断:**记忆 / RAG 是"AI 能力加分项",决定项目能否算"项目"的是工程化层。**
> 建议顺序:**P0 → P1 → P2 → P3**,记忆 / RAG(P4)可与 P1 并行。
> 图例:🟥 必做(不做就不像项目) · 🟨 上线前必做 · 🟩 加分项

---

## P0 · 让项目"立起来"(成本最低、回报最高,建议先做)

- [ ] 🟥 `git init`,把 `backend/venv/`、`backend/.env`、`frontend/node_modules/`、`frontend/.env` 写进 `.gitignore`,做第一次提交(现在整个目录没有版本控制)。
- [ ] 🟥 轮换已经暴露的密钥(ModelScope / 高德 / Unsplash),把 `.env` 从版本控制中移除。
- [ ] 🟥 提供 `backend/.env.example` 和 `frontend/.env.example`,只留键名和注释。
- [ ] 🟥 写 `README.md`:环境要求、必填配置、前后端启动命令、一条 `curl` 示例、常见问题。
- [ ] 🟥 锁定依赖:后端固定 `requirements.txt` 版本(或加 `constraints.txt`),前端已锁定 `package-lock.json` 保持提交。
- [ ] 🟥 一键启动脚本(`scripts/dev.ps1` 或 `Makefile`):同时拉起后端 + 前端。
- [ ] 🟥 最小测试集:
  - [ ] `backend` 单测:`extract_json` / `parse_mcp_payload`、`_parse_weather`、`_parse_pois`。
  - [ ] `backend` 冒烟:`TestClient` 打桩 LLM,验证 `GET /health`、`GET /api/trip/health`、`POST /api/trip/plan` 返回结构。
  - [ ] `frontend`:`npm run build`(`vue-tsc`)作为最低门槛的 CI 检查。

**P0 完成定义:** 一个新人在干净机器上,照 README 五步内能把项目跑起来;`git status` 干净;密钥不在库里。

---

## P1 · 能部署、能稳定运行

- [ ] 🟨 Docker 化:`backend/Dockerfile`、`frontend/Dockerfile` + `docker-compose.yml`(含前后端;高德 MCP 依赖的 `uvx` 要装进镜像)。
- [ ] 🟨 关掉生产环境的 `run.py` 里 `reload=True`,用环境区分 dev / prod 配置。
- [ ] 🟨 结构化日志:把散落的 `print` + emoji 换成 `loguru`(已在依赖中),补 request id、耗时、降级标记。
- [ ] 🟨 **修掉"假数据当成功"**:`_create_fallback_plan` 会返回 `"{城市}景点1"` 这类占位内容却仍 `success=True`;应显式标记 `degraded=True` 或直接返回失败。
- [ ] 🟨 健康检查校验真实依赖:现在 `/api/map/health` 只看 key 是否存在,应实际 ping 一次 MCP / LLM。
- [ ] 🟨 限流 + 并发背压 + 超时:一个 `/plan` 请求会触发 4 次大模型 + 多次 `uvx`;加 `Semaphore` / 队列,避免被刷爆。
- [ ] 🟨 成本护栏:按调用量 / token 计费统计,设单用户日配额。
- [ ] 🟨 解决"每次工具调用新起 `uvx` 进程":改常驻 `streamable-http`,或直接用 `httpx` 封装高德 REST 成普通 Tool;并给 POI / 天气加 TTL 缓存。
- [ ] 🟩 结果级缓存:相同 `TripRequest` 命中缓存直返(有会话/个性化时 bypass)。
- [ ] 🟩 `lifespan` 预热工具与图,降低首次请求延迟。

**P1 完成定义:** 一条 `docker compose up` 能在服务器/新机器上跑起来;有日志可查;被刷时不会打爆你的额度。

---

## P2 · 能安全地给别人用

- [ ] 🟨 接口鉴权:给 `/api/trip/plan` 加 API Key 或用户登录(JWT),区分"谁能调用"。
- [ ] 🟨 配额与防滥用:按用户 / IP 限流。
- [ ] 🟨 密钥管理:走环境变量或密钥服务,禁止明文入库。
- [ ] 🟨 隐私与数据合规:记忆系统会存用户偏好与行程,需说明"收集什么、存哪、保留多久、如何删除",并提供删除入口。
- [ ] 🟨 第三方条款:高德 / Unsplash / 模型服务的使用条款与再分发限制过一遍。
- [ ] 🟩 内容安全:模型输出给终端用户时的审核与免责声明。

**P2 完成定义:** 陌生用户能安全使用;你能说清数据流向;出事能定位到人。

---

## P3 · 像个"产品",而不只是"能跑"

- [ ] 🟩 用户体系:登录、我的行程。
- [ ] 🟩 历史 / 收藏 / 分享 / 导出(前端已有 `jspdf` + `html2canvas`,可做 PDF 导出与分享链接)。
- [ ] 🟩 完整的状态设计:加载中、空状态、错误态,配合**流式输出**把"景点检索完成 / 天气完成 / 规划中"逐步推给前端(缓解现在的 120s 空等)。
- [ ] 🟩 移动端适配。
- [ ] 🟩 新手引导 / 示例城市,降低首次使用门槛。

**P3 完成定义:** 不用你讲解,用户自己能完成一次"规划—查看—保存—分享"。

---

## P4 · AI 能力(加分项,可与 P1 并行)

- [ ] 🟩 **多轮记忆**:`TripPlanningState` 加 `session_id` / `messages`,用 LangGraph `checkpointer`(`MemorySaver` → `SqliteSaver` / `PostgresSaver`);`TripRequest` 加可选 `session_id` 保持向后兼容。
- [ ] 🟩 **长期偏好记忆**:用 LangGraph `Store` 保存"经济型酒店 / 不吃辣 / 带小孩"等,`generate_plan` 前检索并注入。
- [ ] 🟩 **RAG 知识库**:Loader → Splitter → 中文 Embedding(BGE 等)→ 向量库(FAISS/Chroma → pgvector);在图中加 `retrieve_knowledge` 节点,与实时 MCP 工具分工,带来源引用。
- [ ] 🟩 **可观测性**:接入 LangSmith / Langfuse,追踪每次工具调用、token、耗时。
- [ ] 🟩 **评测集**:城市 × 偏好的 golden cases,度量 JSON 合法率、坐标一致性、预算完整率、三路检索成功率、端到端时延、兜底率。
- [ ] 🟩 **人机协同**(可选):用 LangGraph `interrupt` 做"用户确认后再继续"。

> 这些做完,是"功能很强的原型";但**只有 P0–P3 做完,它才是"可以交付的项目"**。

---

## 如果只先做 5 件事(最小可行路线)

1. `git init` + `.gitignore` + 首次提交。
2. 轮换密钥 + `.env.example`。
3. 写 `README.md`(能五步跑起来)。
4. 加最基础的测试(解析器 + `/plan` 冒烟)。
5. 修掉兜底假数据,让失败就是失败。

这五件事做完,项目性质就变了;**记忆 / RAG 可以之后再从容做。**

---

## 一句话原则

> 先让它**能被别人跑起来、能安全上线、出问题能查**,再谈让它**更聪明**(记忆 / RAG / 评测)。