"""LLM 抽象层。

设计目标：项目既能接真实大模型（生产/演示用），也能在无 API Key 时离线运行（mock 模式）。
- 设置了 OPENAI_API_KEY 或 LLM_API_KEY：走 OpenAI 兼容接口（可配 LLM_BASE_URL 指向任意兼容服务）。
- 未设置：available 为 False，各 Agent 自动回退到规则知识库，保证 `docker compose up` 开箱即跑。
"""

import json
import os
import re
from typing import Optional

from openai import OpenAI


def extract_json(text: str) -> Optional[dict]:
    """从模型输出里尽量抠出 JSON 对象。

    模型常会附带解释文字或 ```json 代码块，这里做容错提取：
    去掉 markdown 围栏、截取第一个 { 到最后一个 }，再 json.loads。
    任何解析失败都返回 None，交由调用方回退到规则库。
    """
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        return None
    try:
        return json.loads(text[s : e + 1])
    except (json.JSONDecodeError, ValueError):
        return None


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
        """统一入口：可用时调用真实模型，否则返回空字符串让调用方走规则分支。

        任何网络/鉴权/限流异常都捕获并返回空串，调用方据此安全回退到规则库，
        不会让一次 API 抖动把整条流水线打挂。
        """
        if not self._client:
            return ""
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except Exception as exc:  # noqa: BLE001 - 在线模式需对外部服务容错
            print(f"[LLM] 调用失败，回退规则库：{type(exc).__name__}: {exc}")
            return ""
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
