"""agent 工具作用域：每个 agent 只拿到自己需要的 LLM 可见工具名列表。

确定性业务函数在 services.py，不走这里。
"""

from .registry import get_tool

# agent 名 → LLM 可选工具名列表（防止跨域误调）
AGENT_TOOLS: dict[str, list[str]] = {
    "diagnose": ["lookup_kb", "read_sensor", "recall_cases"],
}


def get_tools_for_agent(agent_name: str) -> list:
    """返回指定 agent 可用的工具对象列表。"""
    return [get_tool(n) for n in AGENT_TOOLS.get(agent_name, [])]
