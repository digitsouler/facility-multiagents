"""Agent 侧 MCP 工具封装：在 Agent 内调用，带离线回退。

- 仅对「能映射到受监控资产」的工单拉取 IoT 实时遥测（其余类型如电梯/保洁/绿化
  无对应传感器，自动跳过，走 KB）。
- 任何异常（server 未起 / 超时 / 解析失败）都返回 None/[]，Agent 据此回退，
  绝不中断流水线。
"""
from .hub import get_hub

# 仅这些故障类型可能存在受监控资产；其余类型无对应传感器
_IOT_TYPES = {"hvac", "leak", "lighting", "fire"}

# 工单 location_hint -> IoT 资产 ID（仅保留语义明确、不会误关联的映射）
_LOCATION_TO_ASSET = {
    "12楼": "ahu-12f",
    "大堂": "ahu-lobby",
    "会议室": "fancoil-meeting",
    "地下车库": "pipe-b2",
    "B栋": "pipe-b1",
    "地库": "meter-b2",
    "三楼": "smoke-3f",
}


def _resolve_asset(ticket: dict):
    if ticket.get("type") not in _IOT_TYPES:
        return None
    return _LOCATION_TO_ASSET.get(ticket.get("location_hint"))


def read_sensor_for_ticket(ticket: dict):
    """返回该工单对应资产的实时遥测 dict，或 None（无匹配 / 服务不可用）。"""
    asset_id = _resolve_asset(ticket)
    if not asset_id:
        return None
    try:
        hub = get_hub()
        if not hub.available("iot"):
            return None
        res = hub.call_tool("iot", "read_sensor", {"asset_id": asset_id, "metric": "all"})
        if isinstance(res, dict) and "error" not in res:
            return res
        return None
    except Exception:  # noqa: BLE001
        return None


def read_anomalies():
    """返回当前异常资产列表，失败返回 []。"""
    try:
        hub = get_hub()
        if not hub.available("iot"):
            return []
        res = hub.call_tool("iot", "list_anomalies", {})
        return res if isinstance(res, list) else []
    except Exception:  # noqa: BLE001
        return []
