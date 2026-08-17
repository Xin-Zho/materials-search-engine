"""诊断 term matrix 解析失败：打印 LLM 完整响应 + finish_reason + json 错误。"""

import asyncio
import json
import os
import httpx
from search_engine.term_matrix import MATRIX_PROMPT
from search_engine.knowledge import get_domain_context


async def main():
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    prompt = MATRIX_PROMPT.format(
        question="光固化聚合物降低聚合收缩与收缩应力的机制",
        domain_context=get_domain_context("photocuring"),
    )

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": "You are a literature search strategist. Output only valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": 4096,
            },
        )
        data = resp.json()
        choice = data["choices"][0]
        content = choice["message"]["content"]
        finish_reason = choice.get("finish_reason")

        print(f"finish_reason: {finish_reason}")
        print(f"content 长度: {len(content)} 字符")
        print(f"usage: {data.get('usage')}")
        print("\n=== 完整响应 ===")
        print(content)
        print("\n=== json.loads 尝试 ===")
        try:
            obj = json.loads(content)
            print("JSON 合法，keys:", list(obj.keys()))
            print("strategy_route:", obj.get("strategy_route"))
        except json.JSONDecodeError as e:
            print(f"JSON 错误: {e}")
            # 打印出错位置附近
            pos = e.pos if hasattr(e, 'pos') else 0
            print(f"出错位置附近: ...{content[max(0,pos-50):pos+50]}...")


if __name__ == "__main__":
    asyncio.run(main())
