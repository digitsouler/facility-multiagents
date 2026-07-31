"""本地 MCP server（stdio，真实协议 + 模拟数据）。

模拟 IoT 传感器接入点：暴露 read_sensor(fault_type) 返回该故障类型下多条资产的实时读数
（每条资产一行，故称"两条传感器数据"= 两个资产读数）。
将来接真系统（真实网关/PLC/SCADA）只需改本文件的数据来源，server 协议与客户端不变。
"""

import asyncio
import json
import logging

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

log = logging.getLogger("facilitymind.mcp.server")

# 每种故障类型下若干资产的传感器读数（2 行为主，模拟多点位）
SENSOR_DATA: dict[str, list[dict]] = {
    "elevator": [
        {"asset": "A座3#梯", "door_current_A": 28.5, "vibration_mm_s": 4.2, "cage_temp_C": 31.0, "status": "异常"},
        {"asset": "B座1#梯", "door_current_A": 12.1, "vibration_mm_s": 1.1, "cage_temp_C": 28.0, "status": "正常"},
    ],
    "hvac": [
        {"asset": "2F-VRV-07", "supply_temp_C": 19.5, "return_temp_C": 27.8, "pressure_kPa": 42, "status": "异常"},
        {"asset": "3F-VRV-12", "supply_temp_C": 16.2, "return_temp_C": 24.1, "pressure_kPa": 55, "status": "正常"},
    ],
    "leak": [
        {"asset": "B1-给水管-03", "flow_L_min": 0.0, "pressure_kPa": 8, "leak_detect": True, "status": "异常"},
        {"asset": "B2-给水管-11", "flow_L_min": 12.0, "pressure_kPa": 310, "leak_detect": False, "status": "正常"},
    ],
    "lighting": [
        {"asset": "1F-配电-05", "voltage_V": 228, "current_A": 0.0, "breaker": "trip", "status": "异常"},
        {"asset": "1F-配电-06", "voltage_V": 221, "current_A": 3.4, "breaker": "on", "status": "正常"},
    ],
    "fire": [
        {"asset": "5F-烟感-22", "smoke_val": 0.02, "battery_V": 2.6, "status": "欠压"},
        {"asset": "5F-烟感-23", "smoke_val": 0.01, "battery_V": 3.1, "status": "正常"},
    ],
    "access": [
        {"asset": "东门闸机-01", "comm_ok": False, "last_event": "超时", "status": "异常"},
        {"asset": "东门闸机-02", "comm_ok": True, "last_event": "正常刷卡", "status": "正常"},
    ],
    "cleaning": [
        {"asset": "3F-公区-01", "trash_level": 0.9, "last_clean_h": 26, "status": "待清理"},
        {"asset": "3F-公区-02", "trash_level": 0.2, "last_clean_h": 4, "status": "正常"},
    ],
    "greening": [
        {"asset": "中庭草坪-A", "soil_moist": 12, "pest": True, "status": "异常"},
        {"asset": "中庭草坪-B", "soil_moist": 35, "pest": False, "status": "正常"},
    ],
    "charging": [
        {"asset": "B2-充电桩-01", "gun_temp_C": 68, "output_current_A": 0.0, "error_code": "E03过温", "status": "异常"},
        {"asset": "B2-充电桩-02", "gun_temp_C": 42, "output_current_A": 32.0, "error_code": "无", "status": "正常"},
    ],
}


app = Server("facility-iot")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="read_sensor",
            description="读取某故障类型下各资产的 IoT 传感器实时读数，返回资产列表（含状态）。fault_type 用标准化 key（如 elevator）。",
            inputSchema={
                "type": "object",
                "properties": {"fault_type": {"type": "string", "description": "标准化故障类型 key"}},
                "required": ["fault_type"],
            },
        ),
        types.Tool(
            name="list_sensors",
            description="列出当前支持的故障类型与各类型下资产数量。",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "read_sensor":
        ft = str(arguments.get("fault_type", "cleaning"))
        rows = SENSOR_DATA.get(ft, SENSOR_DATA["cleaning"])
        log.info("read_sensor(%s) → %d rows", ft, len(rows))
        return [types.TextContent(type="text", text=json.dumps({"fault_type": ft, "rows": rows}, ensure_ascii=False))]
    if name == "list_sensors":
        summary = {k: len(v) for k, v in SENSOR_DATA.items()}
        return [types.TextContent(type="text", text=json.dumps(summary, ensure_ascii=False))]
    raise ValueError(f"未知工具: {name}")


async def _main() -> None:
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(_main())
