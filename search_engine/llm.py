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
        # 最近一次调用的 usage（token 计数）。**只增不改**：调用方用
        # getattr(backend, "last_usage", None) 读，用于成本核算。
        # 不改签名、不改返回值，对所有既有调用方零影响。
        self.last_usage = None

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
        # 可选 JSON mode（强制合法 JSON，防止 degenerate 无限列举）
        if raise_on_truncation:
            body["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/v1/chat/completions",
                headers=headers,
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            self.last_usage = data.get("usage")
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


class TencentMaasBackend(LLMBackend):
    """腾讯云 tokenhub（OpenAI 兼容接口，hy4-preview 等混元模型）。

    与 DeepSeekBackend 同构（/v1/chat/completions + Bearer），仅默认参数不同。
    """

    def __init__(self, api_key: str, model: str = "hy4-preview",
                 base_url: str = "https://tokenhub.tencentmaas.com"):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    async def chat(self, system_prompt: str, user_message: str,
                   temperature: float = 0.3, max_tokens: int = 2048,
                   raise_on_truncation: bool = False) -> str:
        import asyncio
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
        if raise_on_truncation:
            body["response_format"] = {"type": "json_object"}

        # 429/5xx 指数退避重试（tokenhub 混元限流窗口可能较长，实测连续 429）
        # 退避 [2, 5, 10, 20] 秒；优先尊重服务端 Retry-After header
        last_resp = None
        for attempt in range(5):
            async with httpx.AsyncClient(timeout=180) as client:
                resp = await client.post(
                    f"{self.base_url}/v1/chat/completions",
                    headers=headers,
                    json=body,
                )
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                    wait = (2, 5, 10, 20)[attempt]    # 指数退避 2,5,10,20s
                    ra = resp.headers.get("Retry-After")
                    if ra and ra.isdigit():
                        wait = min(int(ra), 60)
                    logger.warning("TencentMaas %s 限流/暂不可用，%.0fs 后重试 "
                                   "(%d/4)", resp.status_code, wait, attempt + 1)
                    await asyncio.sleep(wait)
                    last_resp = resp
                    continue
                resp.raise_for_status()
                data = resp.json()
                choice = data["choices"][0]
                finish_reason = choice.get("finish_reason", "stop")
                if raise_on_truncation and finish_reason == "length":
                    raise TruncatedResponse(
                        f"TencentMaas 输出被截断（finish_reason=length, "
                        f"max_tokens={max_tokens}）"
                    )
                return choice["message"]["content"]
        raise RuntimeError(
            f"TencentMaas 重试 4 次仍 429/5xx（tokenhub 速率配额耗尽）——等待 1-2 分钟"
            f"再跑，或换 --provider deepseek。最后 HTTP "
            f"{last_resp.status_code if last_resp else '?'}")


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
    elif provider in ("tencent", "maas", "tencentmaas"):
        return TencentMaasBackend(
            api_key=api_key or "",
            model=model or "hy4-preview",
            base_url=base_url or "https://tokenhub.tencentmaas.com",
        )
    else:
        raise ValueError(f"未支持的 provider: {provider}")
