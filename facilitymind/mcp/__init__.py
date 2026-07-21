"""FacilityMind MCP 接入层。

对外暴露：
    get_hub()        获取进程内 MCPHub 单例（按 mcp.json 拉起 server 子进程）
    MCPHub           手动创建 hub
    providers        供 Agent 直接调用的工具封装（带离线回退）

设计要点：Agent 通过确定性工具调用（先拉真实数据喂 LLM）接入 MCP，
server 不可用 / 未配置时自动回退 KB，整条流水线不中断。
"""
from .hub import MCPHub, get_hub
from . import providers

__all__ = ["MCPHub", "get_hub", "providers"]
