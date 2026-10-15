"""配置管理模块"""

import os
from pathlib import Path
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

# 加载环境变量
# 首先尝试加载当前目录的.env
load_dotenv()

# 然后尝试加载同级的 HelloAgents 仓库 .env(如果存在,用于兼容旧配置)
legacy_env = Path(__file__).parent.parent.parent.parent / "HelloAgents" / ".env"
if legacy_env.exists():
    load_dotenv(legacy_env, override=False)  # 不覆盖已有的环境变量


class Settings(BaseSettings):
    """应用配置"""

    # 应用基本配置
    app_name: str = "智能旅行规划助手"
    app_version: str = "1.0.0"
    debug: bool = False
    # 运行环境:development / production。决定是否开启热重载与调试日志。
    app_env: str = "development"

    # 服务器配置
    host: str = "0.0.0.0"
    port: int = 8000

    # CORS配置 - 使用字符串,在代码中分割
    cors_origins: str = "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173,http://127.0.0.1:3000"

    # 高德地图API配置
    amap_api_key: str = ""

    # Unsplash API配置
    unsplash_access_key: str = ""
    unsplash_secret_key: str = ""

    # LLM配置 (LangChain ChatOpenAI)
    # 统一走 OpenAI 兼容协议,与 .env 中的 LLM_MODEL_ID / LLM_BASE_URL 对应
    llm_model_id: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    modelscope_api_key: str = ""
    llm_temperature: float = 0.7
    llm_timeout: int = 60
    # Qwen3.5 默认开启 thinking(思考)模式,而该模式下服务端会拒绝
    # tool_choice=required/object,导致 response_format 结构化输出直接 400。
    # 关掉它既能让结构化输出生效,也能显著降低 4 次模型调用的耗时。
    # 注意:enable_thinking 是 Qwen(ModelScope/DashScope)专用参数,
    # 若换成其它 OpenAI 兼容服务商并遇到 400,请一并调整 llm_service。
    llm_enable_thinking: bool = False

    # 兼容旧字段(保留以免破坏其他引用)
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4"

    # 日志配置
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",  # 忽略额外的环境变量
    )

    def get_cors_origins_list(self) -> List[str]:
        """获取CORS origins列表"""
        return [origin.strip() for origin in self.cors_origins.split(',')]

    @property
    def is_development(self) -> bool:
        """是否运行在开发环境(决定 uvicorn 是否开启 reload)。"""
        return self.app_env.strip().lower() in {"dev", "development", "local"}

    def resolve_llm_credentials(self) -> tuple:
        """解析 LLM 凭据

        按优先级从配置/环境变量中取出 (model, api_key, base_url),
        与旧版 HelloAgentsLLM 的多 provider 推断逻辑等价。

        Returns:
            (model, api_key, base_url) 三元组
        """
        api_key = (
            self.llm_api_key
            or self.modelscope_api_key
            or os.getenv("MODELSCOPE_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or ""
        )
        model = (
            self.llm_model_id
            or os.getenv("LLM_MODEL_ID")
            or self.openai_model
        )
        base_url = (
            self.llm_base_url
            or os.getenv("LLM_BASE_URL")
            or ""
        )
        return model, api_key, base_url


# 创建全局配置实例
settings = Settings()


def get_settings() -> Settings:
    """获取配置实例"""
    return settings


# 验证必要的配置
def validate_config():
    """验证配置是否完整"""
    errors = []
    warnings = []

    if not settings.amap_api_key:
        errors.append("AMAP_API_KEY未配置")

    # LLM 凭据按优先级解析,缺失时只告警不阻断启动
    _, llm_api_key, _ = settings.resolve_llm_credentials()
    if not llm_api_key:
        warnings.append(
            "LLM API Key 未配置(LLM_API_KEY / MODELSCOPE_API_KEY),LLM功能可能无法使用"
        )

    if errors:
        error_msg = "配置错误:\n" + "\n".join(f"  - {e}" for e in errors)
        raise ValueError(error_msg)

    if warnings:
        print("\n⚠️  配置警告:")
        for w in warnings:
            print(f"  - {w}")

    return True


# 打印配置信息(用于调试)
def print_config():
    """打印当前配置(隐藏敏感信息)"""
    print(f"应用名称: {settings.app_name}")
    print(f"版本: {settings.app_version}")
    print(f"服务器: {settings.host}:{settings.port}")
    print(f"高德地图API Key: {'已配置' if settings.amap_api_key else '未配置'}")

    # 检查LLM配置
    llm_model, llm_api_key, llm_base_url = settings.resolve_llm_credentials()

    print(f"LLM API Key: {'已配置' if llm_api_key else '未配置'}")
    print(f"LLM Base URL: {llm_base_url}")
    print(f"LLM Model: {llm_model}")
    print(f"日志级别: {settings.log_level}")

