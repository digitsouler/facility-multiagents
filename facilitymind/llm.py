"""LLM 抽象层。

设计目标：项目既能接真实大模型（生产/演示用），也能在无 API Key 时离线运行（mock 模式）。
- 设置了 OPENAI_API_KEY 或 LLM_API_KEY：走 OpenAI 兼容接口（可配 LLM_BASE_URL 指向任意兼容服务）。
- 未设置：available 为 False，各 Agent 自动回退到规则知识库，保证 `docker compose up` 开箱即跑。
"""

import os
from typing import Optional

from openai import OpenAI


class LLMClient:
    def __init__(self) -> None:
        self.api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL")
        self.model = os.getenv("LLM_MODEL", "gpt-4o-mini")
        self._client: Optional[OpenAI] = None
        self.total_tokens: int = 0   # 累计 token 消耗（供评估 harness 计量）
        self.call_count: int = 0      # 累计调用次数
        if self.api_key:
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url or None)

    def reset(self) -> None:
        """每轮评估前清零计量，便于按工单统计。"""
        self.total_tokens = 0
        self.call_count = 0

    @property
    def available(self) -> bool:
        return self._client is not None

    def complete(self, system: str, user: str, temperature: float = 0.0) -> str:
        """统一入口：可用时调用真实模型，否则返回空字符串让调用方走规则分支。"""
        if not self._client:
            return ""
        resp = self._client.chat.completions.create(
            model=self.model,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        # 计量：优先用接口返回的 usage，缺失时按字符粗估（中文约 1 token/2 字）
        usage = getattr(resp, "usage", None)
        if usage is not None and getattr(usage, "total_tokens", None) is not None:
            self.total_tokens += int(usage.total_tokens)
        else:
            self.total_tokens += max(1, len(system) // 2 + len(user) // 2)
        self.call_count += 1
        return resp.choices[0].message.content or ""


# 模块级单例：各 Agent 直接 import 使用，降低 MVP 复杂度。
llm = LLMClient()
