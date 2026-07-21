"""加载 mcp.json，解析 MCP server 清单。

配置位于项目根目录（facilitymind 包的上一级）。每个 server 至少含：
    name      唯一名
    transport stdio（当前支持）/ http（预留）
    module    包内 server 模块，如 facilitymind.mcp.servers.iot
    enabled   是否启用
"""
import json
import os

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(PKG_DIR)
CONFIG_PATH = os.path.join(ROOT_DIR, "mcp.json")


def load_config(path: str = CONFIG_PATH) -> dict:
    if not os.path.exists(path):
        return {"servers": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_command(spec: dict):
    """把 spec 解析为 (command, args)。

    优先用 spec['command']；否则用 module 解析为包内文件路径，
    以当前解释器（venv python）直接运行，避免 PYTHONPATH 问题。
    """
    import sys

    if spec.get("command"):
        return spec["command"], list(spec.get("args", []))

    module = spec.get("module")  # e.g. facilitymind.mcp.servers.iot
    rel = module.replace(".", os.sep) + ".py"
    file_path = os.path.join(ROOT_DIR, rel)
    return sys.executable, [file_path]
