"""LLM 后端统一接口。支持 DeepSeek API 和 Ollama 本地模型。

使用方式:
    backend = DeepSeekBackend(api_key="sk-...")
    # 或
    backend = OllamaBackend(model="qwen3:8b")

    response = await backend.chat("system prompt", "user message")
"""

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class TruncatedResponse(Exception):
    """LLM 输出被截断（finish_reason=length），结果无效，需重试。"""
    pass


class LLMBackend(ABC):
    """LLM 后端基类。"""

    @abstractmethod
    async def chat(self, system_prompt: str, user_message: str,
                   temperature: float = 0.3, max_tokens: int = 2048) -> str:
        """发送消息，返回文本响应。"""
        ...


class DeepSeekBackend(LLMBackend):
    """DeepSeek API (deepseek-chat)。"""

    def __init__(self, api_key: str, model: str = "deepseek-chat",
                 base_url: str = "https://api.deepseek.com"):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    async def chat(self, system_prompt: str, user_message: str,
                   temperature: float = 0.3, max_tokens: int = 2048,
                   raise_on_truncation: bool = False) -> str:
        import httpx

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/v1/chat/completions",
                headers=headers,
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]
            finish_reason = choice.get("finish_reason", "stop")
            if raise_on_truncation and finish_reason == "length":
                raise TruncatedResponse(
                    f"DeepSeek 输出被截断（finish_reason=length, max_tokens={max_tokens}）"
                )
            return choice["message"]["content"]


class OllamaBackend(LLMBackend):
    """Ollama 本地模型 (qwen2.5, llama, etc.)。"""

    def __init__(self, model: str = "qwen2.5:7b",
                 base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url

    async def chat(self, system_prompt: str, user_message: str,
                   temperature: float = 0.3, max_tokens: int = 2048) -> str:
        import httpx

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }

        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"{self.base_url}/api/chat",
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["message"]["content"]


def create_backend(
    provider: str = "deepseek",
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> LLMBackend:
    """工厂函数：根据 provider 创建后端。"""
    if provider == "deepseek":
        return DeepSeekBackend(
            api_key=api_key or "",
            model=model or "deepseek-chat",
            base_url=base_url or "https://api.deepseek.com",
        )
    elif provider == "ollama":
        return OllamaBackend(
            model=model or "qwen2.5:7b",
            base_url=base_url or "http://localhost:11434",
        )
    else:
        raise ValueError(f"未支持的 provider: {provider}")
