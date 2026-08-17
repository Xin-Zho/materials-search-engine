"""诊断 term matrix 输出稳定性：跑 N 次，看 json 合法率 + 失败响应长什么样。"""

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

    n = 5
    ok = 0
    async with httpx.AsyncClient(timeout=120) as client:
        for i in range(n):
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
            finish = choice.get("finish_reason")

            try:
                json.loads(content)
                ok += 1
                print(f"[{i+1}] OK  (finish={finish}, len={len(content)})")
            except json.JSONDecodeError as e:
                print(f"[{i+1}] FAIL (finish={finish}, len={len(content)}, err={e})")
                # 打印出错位置附近
                pos = e.pos if hasattr(e, 'pos') else 0
                print(f"      出错附近: ...{content[max(0,pos-40):pos+40]}...")
                # 打印结尾（截断通常在这里）
                print(f"      结尾: ...{content[-120:]}")

    print(f"\njson 合法率: {ok}/{n}")


if __name__ == "__main__":
    asyncio.run(main())
