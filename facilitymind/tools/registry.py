"""LLM 可见工具注册中心。

只放需要在 ReAct 中被 LLM 自主选择的检索/取证工具：
- lookup_kb：查故障知识库
- read_sensor：经 MCP 读 IoT 传感器
- recall_cases：检索相似历史案例

确定性业务计算（预算/SLA/排序/质检/案例写入）放在 services.py，由 agent 直接调用。
"""

import json
import logging

from langchain_core.tools import tool

from ..knowledge import KB
from ..memory.qdrant_cases import recall_cases as _qdrant_recall

log = logging.getLogger("facilitymind.tools")


@tool
def lookup_kb(fault_type: str) -> str:
    """查故障知识库，返回该类型的根因/建议/所需技能/预估成本/SLA。fault_type 用标准化英文 key（如 elevator、hvac）。"""
    data = KB.get(fault_type, KB["cleaning"])
    log.info("[tool] lookup_kb(%s) → %s", fault_type, list(data))
    return json.dumps(data, ensure_ascii=False)


@tool
def read_sensor(fault_type: str) -> str:
    """经 MCP 读取 IoT 传感器实时读数（真实协议，本地 server 模拟数据），返回该故障类型下各资产读数。
    fault_type 用标准化 key。MCP 不可用时自动回退模拟桩。"""
    from .mcp_client import read_iot_sensor

    data = read_iot_sensor(fault_type)
    rows = data.get("rows", [])
    log.info("[tool] read_sensor(%s) 来源=%s 行数=%d → %s",
             fault_type, data.get("source"), len(rows), rows)
    return json.dumps(data, ensure_ascii=False)


@tool
def recall_cases(fault_type: str, location: str = "") -> str:
    """检索相似历史工单（好案例优先）。在线走 Qdrant 向量语义检索，离线回退本地 JSONL 关键词。"""
    cases = _qdrant_recall(fault_type, location, top_k=3, good_only=True)
    log.info("[tool] recall_cases(%s, %s) → %d 命中", fault_type, location, len(cases))
    return json.dumps(cases, ensure_ascii=False)


_REGISTRY: dict = {
    "lookup_kb": lookup_kb,
    "read_sensor": read_sensor,
    "recall_cases": recall_cases,
}


def get_tool(name: str):
    return _REGISTRY[name]


def all_tools() -> list:
    return list(_REGISTRY.values())


def call_tool(name: str, args: dict) -> dict:
    """安全调用工具并返回 dict，处理 JSON 反序列化与异常。"""
    tool_fn = _REGISTRY.get(name)
    if not tool_fn:
        log.error("[call_tool] 未知工具：%s", name)
        return {"error": f"unknown tool {name}"}
    try:
        result = tool_fn.invoke(args) if hasattr(tool_fn, "invoke") else tool_fn(**args)
        return json.loads(result) if isinstance(result, str) else (result or {})
    except (json.JSONDecodeError, TypeError) as exc:
        log.error("[call_tool] %s 返回非 JSON：%s", name, exc)
        return {"error": f"bad json from {name}", "raw": str(result)}
    except Exception as exc:  # noqa: BLE001
        log.error("[call_tool] %s 执行失败：%s", name, exc)
        return {"error": f"tool {name} failed", "detail": str(exc)}
