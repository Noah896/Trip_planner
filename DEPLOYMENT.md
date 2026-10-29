# 部署说明

本文面向使用 Docker Compose 的单实例自托管部署。详细功能、配置和限制见根目录 `README.md`。

## 前置条件

- Docker Engine 24+ 与 Docker Compose v2
- 可访问模型服务、高德服务和 HuggingFace / PyPI 的网络
- 高德后端 Key、高德 Web 端 JS Key、LLM API Key

## 首次部署

```bash
cp .env.compose.example .env
cp backend/.env.example backend/.env
```

编辑 `backend/.env`：

```dotenv
LLM_API_KEY=...
LLM_BASE_URL=https://api-inference.modelscope.cn/v1
LLM_MODEL_ID=Qwen/Qwen3.5-27B
AMAP_API_KEY=...
AUTH_ADMIN_KEY=至少32字符的高熵随机值
```

编辑根目录 `.env`：

```dotenv
APP_PORT=8080
PUBLIC_ORIGIN=http://localhost:8080
AUTH_ALLOW_REGISTRATION=false
VITE_AMAP_WEB_JS_KEY=...
```

然后启动：

```bash
docker compose up --build -d
docker compose ps
curl --fail http://localhost:8080/health
```

首次启动时，RAG 会下载 `BAAI/bge-small-zh-v1.5`，首次规划还可能通过 `uvx` 下载高德 MCP Server。后端健康检查给模型下载留出了 3 分钟启动时间。

## 更新与回滚

```bash
docker compose build --pull
docker compose up -d
docker compose ps
```

数据库和模型缓存存放在命名卷中。回滚应用版本时不需要回滚卷，除非新版本包含不可逆的数据结构变更。

## 公网安全

- 只公开前端 Nginx 端口；后端端口默认只绑定宿主机回环地址。
- 使用 Caddy、Traefik、Nginx 或云负载均衡终止 TLS。
- 关闭匿名注册，或设置 `AUTH_ALLOW_REGISTRATION=true` 和强 `AUTH_REGISTRATION_KEY`。
- 设置至少 32 字符的 `AUTH_ADMIN_KEY`，不要与管理接口分离部署。
- 在反向代理或云平台配置请求速率、最大并发、请求体大小和模型费用告警。
- 不要把 `.env`、SQLite 文件或日志提交到 Git。

## 备份

先停止写入，再导出数据卷：

```bash
docker compose stop backend
docker run --rm \
  -v trip_planner_trip_data:/data \
  -v "$PWD":/backup \
  alpine tar czf /backup/trip_data_backup.tar.gz -C /data .
docker compose start backend
```

卷名前缀可能随 Compose 项目名变化，可先用 `docker volume ls` 确认。

## 当前部署边界

该配置适合作品演示和单实例自托管，不等同于生产高可用：

- SQLite 不支持多实例共享写入。
- 没有全局模型并发背压、结构化日志和集中式指标。
- 没有容器级资源限额和多节点调度。
- 公开部署前必须做费用、并发和故障演练。
