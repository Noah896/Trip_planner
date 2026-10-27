"""FastAPI主应用"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import get_settings, print_config, validate_config
from ..services.amap_mcp import shutdown_amap_server
from ..services.knowledge_retrieval import initialize_knowledge_service
from .routes import auth
from .routes import chat
from .routes import history
from .routes import map as map_routes
from .routes import memory, poi, trip

# 获取配置
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期(替代已弃用的 @app.on_event)"""
    print("\n" + "=" * 60)
    print(f"🚀 {settings.app_name} v{settings.app_version}")
    print("=" * 60)

    # 打印配置信息
    print_config()

    # 验证配置
    try:
        validate_config()
        print("\n✅ 配置验证通过")
    except ValueError as e:
        print(f"\n❌ 配置验证失败:\n{e}")
        print("\n请检查.env文件并确保所有必要的配置项都已设置")
        raise

    if settings.rag_enabled:
        print("\n📚 正在预热 BGE / FAISS / BM25 知识检索...")
        try:
            initialize_knowledge_service()
            print("✅ 知识检索初始化完成")
        except Exception as exc:
            # RAG 是可选增强能力；预热失败不能阻止后端启动。
            print(f"⚠️ 知识检索初始化失败,将在请求时降级: {type(exc).__name__}: {exc}")

    print("\n" + "=" * 60)
    print("📚 API文档: http://localhost:8000/docs")
    print("📖 ReDoc文档: http://localhost:8000/redoc")
    print("=" * 60 + "\n")

    yield

    print("\n" + "=" * 60)
    print("👋 应用正在关闭...")
    print("=" * 60 + "\n")
    # 回收常驻的高德 MCP Server(若由本进程拉起)
    shutdown_amap_server()


# 创建FastAPI应用
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="基于 LangChain + LangGraph 的智能旅行规划助手API",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "Retry-After",
        "X-Model-Quota-Limit",
        "X-Model-Quota-Used",
        "X-Model-Quota-Remaining",
        "X-Model-Quota-Reset-At",
    ],
)

# 注册路由
app.include_router(auth.router, prefix="/api")
app.include_router(trip.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(history.router, prefix="/api")
app.include_router(memory.router, prefix="/api")
app.include_router(poi.router, prefix="/api")
app.include_router(map_routes.router, prefix="/api")


@app.get("/")
async def root():
    """根路径"""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "running",
        "framework": "langchain+langgraph",
        "docs": "/docs",
        "redoc": "/redoc",
    }


@app.get("/health")
async def health():
    """健康检查"""
    return {
        "status": "healthy",
        "service": settings.app_name,
        "version": settings.app_version,
        "framework": "langchain+langgraph",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.api.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.is_development,
    )
