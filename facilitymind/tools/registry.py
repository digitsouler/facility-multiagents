"""工具注册中心：所有 LangGraph @tool 在本文件定义，按名称取用，避免散落到各 agent。

分工：
- 本进程确定性工具（查知识库 / 读传感器桩 / 案例检索桩）用 LangGraph @tool 定义，由子图内
  ToolNode 执行；
- 跨系统工具（IoT / CMMS / ERP / IM）后续走 MCP，届时在 mcp_client 包一层转成同形态 @tool，
  agent 调用方式不变（见后续步骤）。

工具统一返回 JSON 字符串：ToolNode 生成的 ToolMessage.content 为字符串最稳妥，调用方按需 json.loads。
"""

import json
import logging

from langchain_core.tools import tool

from ..knowledge import KB

log = logging.getLogger("facilitymind.tools")


@tool
def lookup_kb(fault_type: str) -> str:
    """查故障知识库，返回该类型的根因/建议/所需技能/预估成本/SLA。fault_type 用标准化英文 key（如 elevator、hvac）。"""
    data = KB.get(fault_type, KB["cleaning"])
    log.info("[tool] lookup_kb(%s) → %s", fault_type, list(data))
    return json.dumps(data, ensure_ascii=False)


@tool
def read_sensor(fault_type: str) -> str:
    """读传感器（桩）：返回确定性读数占位。真实接入时换成 MCP iot.read_sensor。fault_type 用标准化 key。"""
    data = {"fault_type": fault_type, "status": "online", "note": "传感器桩（离线模拟，待接 MCP）"}
    log.info("[tool] read_sensor(%s) → %s", fault_type, data)
    return json.dumps(data, ensure_ascii=False)


@tool
def recall_cases(fault_type: str, location: str = "") -> str:
    """检索相似历史工单（好案例优先）。当前为本地桩返回空列表；后续接 Qdrant 改成语义检索。"""
    # 记忆层（第二步）接入 Qdrant 后，这里改成向量检索返回相似案例 payload。
    log.info("[tool] recall_cases(%s, %s) → []", fault_type, location)
    return json.dumps([], ensure_ascii=False)


_REGISTRY: dict = {
    "lookup_kb": lookup_kb,
    "read_sensor": read_sensor,
    "recall_cases": recall_cases,
}


def get_tool(name: str):
    return _REGISTRY[name]


def all_tools() -> list:
    return list(_REGISTRY.values())
