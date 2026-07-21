"""LLM 抽象层 + 多模型注册表（Model Registry）。

设计目标：
- 既能接真实大模型（生产/演示用），也能在无 API Key 时离线运行（mock 模式）。
- 支持多模型协作：每个 profile（base_url/model/api_key）一个独立客户端，各自统计
  token/成本；通过 `get_client(name)` 按名字取用，不再依赖全局单例。
- 默认 profile 仍由 `.env` 的 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL 构造，行为与旧版一致；
  其余模型（Qwen/智谱/Ollama 等）在 `models.json` 中声明，填了对应 Key 即自动启用。
"""

import json
import os
from typing import Optional

from openai import OpenAI

from .tracer import span


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


import re  # noqa: E402  （放此处仅因 extract_json 用到，保持模块可读）


class LLMClient:
    """单个模型的客户端：独立统计 token 与调用次数。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        name: str = "default",
    ) -> None:
        self.name = name
        self.api_key = api_key
        self.base_url = base_url
        self.model = model or "gpt-4o-mini"
        self._client: Optional[OpenAI] = None
        self.total_tokens: int = 0  # 累计 token 消耗（供评估 harness 计量）
        self.call_count: int = 0  # 累计调用次数
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
        _tok_before = self.total_tokens
        with span(f"LLM:{self.name}", "llm", model=self.model) as s:
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
                print(f"[LLM:{self.name}] 调用失败，回退规则库：{type(exc).__name__}: {exc}")
                if s:
                    s.finish(status="error", error=f"{type(exc).__name__}")
                return ""
            usage = getattr(resp, "usage", None)
            if usage is not None and getattr(usage, "total_tokens", None) is not None:
                self.total_tokens += int(usage.total_tokens)
            else:
                self.total_tokens += max(1, len(system) // 2 + len(user) // 2)
            self.call_count += 1
            _tokens = self.total_tokens - _tok_before
            if s:
                s.finish(output_brief=f"{_tokens} tokens", tokens=_tokens)
            return resp.choices[0].message.content or ""


# --------------------------------------------------------------------------- #
# 多模型注册表（Model Registry）
# --------------------------------------------------------------------------- #

_MODELS_PATH = os.path.join(os.path.dirname(__file__), "models.json")
_REGISTRY: dict[str, LLMClient] = {}


def _load_profiles() -> tuple[dict, str]:
    """读取 models.json；缺失时退回内置默认（仅 deepseek 从 .env 构造）。"""
    try:
        with open(_MODELS_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {"default": "deepseek", "models": {}}
    profiles = cfg.get("models", {}) or {}
    default_name = cfg.get("default") or next(iter(profiles), "deepseek")
    return profiles, default_name


_PROFILES, _DEFAULT = _load_profiles()


def _load_routing() -> tuple[dict, list]:
    """读取 models.json 里的 agent_routing 与 ensemble 配置。"""
    try:
        with open(_MODELS_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    routing = cfg.get("agent_routing", {}) or {}
    ensemble = cfg.get("ensemble", ["deepseek", "qwen"]) or ["deepseek", "qwen"]
    return routing, ensemble


_AGENT_ROUTING, _ENSEMBLE_MODELS = _load_routing()


def get_agent_client(agent: str) -> "LLMClient":
    """返回某个 Agent 绑定的模型客户端（按 models.json 的 agent_routing 分配）。"""
    return get_client(_AGENT_ROUTING.get(agent, _DEFAULT))


def get_ensemble_clients() -> list:
    """返回 Ensemble 参与扇出的可用模型客户端列表（仅含已配置 Key 的）。"""
    return [get_client(n) for n in _ENSEMBLE_MODELS if n in _PROFILES]


def usage_breakdown() -> dict:
    """各模型已消耗 token 快照（供评估 harness 按模型拆分计量）。"""
    return {name: c.total_tokens for name, c in _REGISTRY.items()}


def calls_breakdown() -> dict:
    """各模型已调用次数快照（与 usage_breakdown 配套，供 Dashboard 按模型拆分计量）。"""
    return {name: c.call_count for name, c in _REGISTRY.items()}


def total_tokens_all() -> int:
    return sum(c.total_tokens for c in _REGISTRY.values())


def total_calls_all() -> int:
    return sum(c.call_count for c in _REGISTRY.values())


def available_models() -> list:
    """返回当前已启用（配了 Key）的模型名列表。"""
    return [m["name"] for m in list_models() if m["available"]]


def _build_from_profile(name: str, profile: dict) -> LLMClient:
    """按 profile 从环境变量（或字面量）解析出密钥/地址/模型，构造客户端。"""
    api_key = os.getenv(profile.get("api_key_env", "")) or profile.get("api_key")
    base_url = os.getenv(profile.get("base_url_env", "")) or profile.get("base_url")
    model = os.getenv(profile.get("model_env", "")) or profile.get("model")
    return LLMClient(
        api_key=api_key or None,
        base_url=base_url or None,
        model=model,
        name=name,
    )


def get_client(name: Optional[str] = None) -> LLMClient:
    """按名字取（或惰性创建并缓存）一个模型客户端。

    - name=None / 未声明 / 取不到：回退到默认 profile（deepseek）。
    - 首次访问即构造并缓存，之后复用同一实例（token 统计连续）。
    """
    key = name or _DEFAULT
    if key not in _REGISTRY:
        if key in _PROFILES:
            _REGISTRY[key] = _build_from_profile(key, _PROFILES[key])
        else:
            # 未知名字 → 退回默认，保证任何调用都不崩
            if _DEFAULT not in _REGISTRY:
                _REGISTRY[_DEFAULT] = _build_from_profile(_DEFAULT, _PROFILES.get(_DEFAULT, {}))
            key = _DEFAULT
    return _REGISTRY[key]


def list_models() -> list[dict]:
    """列出全部已声明模型及其可用性（供 CLI / Dashboard 展示）。"""
    out = []
    for name, p in _PROFILES.items():
        c = get_client(name)
        out.append(
            {
                "name": name,
                "label": p.get("label", name),
                "available": c.available,
            }
        )
    return out


def reset_all() -> None:
    """清空所有已缓存客户端的计量。"""
    for c in _REGISTRY.values():
        c.reset()


# 向后兼容：模块级单例保持旧调用方式 `from ..llm import llm` 不变。
llm = get_client("default")
