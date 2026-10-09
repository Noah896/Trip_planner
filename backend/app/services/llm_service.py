"""LLM 服务模块(LangChain ChatOpenAI)

替代原先的 ``HelloAgentsLLM``:两者都是通过 OpenAI 兼容协议访问模型,
区别在于 HelloAgentsLLM 会自动推断 provider,而 ChatOpenAI 需要显式
传入 model / base_url / api_key —— 这些统一由 config 解析。
"""

from __future__ import annotations

from typing import Optional

from langchain_openai import ChatOpenAI

from ..config import get_settings

# 全局 LLM 实例
_llm_instance: Optional[ChatOpenAI] = None


def get_llm() -> ChatOpenAI:
    """获取 ChatOpenAI 实例(单例模式)

    Returns:
        配置好的 ChatOpenAI 实例
    """
    global _llm_instance

    if _llm_instance is None:
        settings = get_settings()
        model, api_key, base_url = settings.resolve_llm_credentials()

        if not api_key:
            raise ValueError(
                "LLM API Key 未配置,请在 .env 文件中设置 LLM_API_KEY "
                "或 MODELSCOPE_API_KEY"
            )

        _llm_instance = ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url or None,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout,
        )

        print("✅ LLM 服务初始化成功 (LangChain ChatOpenAI)")
        print(f"   模型: {model}")
        print(f"   服务地址: {base_url}")

    return _llm_instance


def reset_llm() -> None:
    """重置 LLM 实例(用于测试或重新配置)"""
    global _llm_instance
    _llm_instance = None

