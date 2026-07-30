"""每个 agent 只拿自己名下的 2-4 个工具，作用域隔离降幻觉。

后续加工具只需两步：① 在 registry.py 定义 @tool；② 把工具名加入对应 agent 的列表。
agent 业务代码无需改动——它只通过 get_tools_for_agent(名字) 拿到自己的工具集。
"""

from .registry import get_tool

# agent 名 → 它能用的工具名列表（充当时 LLM 的"可见工具面"，防止跨域误调）
AGENT_TOOLS: dict[str, list[str]] = {
    "intake": [],
    "diagnose": ["lookup_kb", "read_sensor", "recall_cases"],
    "dispatch": [],          # 第二步加 rank_vendors（接 Redis 口碑）
    "approval": [],
    "technician_report": [],
    "qa": [],
    "report": [],
}


def get_tools_for_agent(agent: str) -> list:
    return [get_tool(n) for n in AGENT_TOOLS.get(agent, [])]
