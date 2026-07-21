"""本地 IoT MCP server（模拟真实传感器数据，真实 MCP 协议，可离线运行）。

由 MCPHub 以子进程方式拉起（stdio 传输）。Agent 可通过 read_sensor / list_assets
/ list_anomalies 三个工具获取实时遥测。将来要接真实 IoT 平台，只需在 mcp.json
里把 module 换成真实 server 的启动命令即可，Agent 侧代码无需改动。

工具：
    read_sensor(asset_id, metric="all") -> dict
    list_assets()                        -> list
    list_anomalies()                     -> list
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("facility-iot")

# 模拟资产实时遥测（确定性数据，便于演示复现）。
# 每个指标含 value / unit / baseline / status；status=anomaly 表示偏离基线需关注。
ASSETS = {
    "ahu-12f": {
        "asset_id": "ahu-12f", "name": "12楼空调机房 AHU", "location": "12楼", "type": "hvac",
        "metrics": {
            "supply_air_temp": {"value": 29.5, "unit": "℃", "baseline": 18.0, "status": "anomaly"},
            "return_air_temp": {"value": 27.0, "unit": "℃", "baseline": 25.0, "status": "normal"},
            "energy_kwh": {"value": 14.2, "unit": "kWh", "baseline": 12.0, "status": "normal"},
            "filter_pressure": {"value": 180.0, "unit": "Pa", "baseline": 120.0, "status": "anomaly"},
        },
    },
    "ahu-lobby": {
        "asset_id": "ahu-lobby", "name": "大堂新风机 AHU", "location": "大堂", "type": "hvac",
        "metrics": {
            "supply_air_temp": {"value": 30.0, "unit": "℃", "baseline": 19.0, "status": "anomaly"},
            "return_air_temp": {"value": 28.0, "unit": "℃", "baseline": 26.0, "status": "normal"},
            "energy_kwh": {"value": 9.8, "unit": "kWh", "baseline": 9.0, "status": "normal"},
            "filter_pressure": {"value": 215.0, "unit": "Pa", "baseline": 110.0, "status": "anomaly"},
        },
    },
    "fancoil-meeting": {
        "asset_id": "fancoil-meeting", "name": "会议室风机盘管", "location": "会议室", "type": "hvac",
        "metrics": {
            "supply_air_temp": {"value": 28.3, "unit": "℃", "baseline": 17.0, "status": "anomaly"},
            "valve_open": {"value": 40, "unit": "%", "baseline": 95, "status": "anomaly"},
            "energy_kwh": {"value": 3.1, "unit": "kWh", "baseline": 3.0, "status": "normal"},
        },
    },
    "pipe-b2": {
        "asset_id": "pipe-b2", "name": "地下车库供水管", "location": "地下车库", "type": "leak",
        "metrics": {
            "pressure": {"value": 1.8, "unit": "bar", "baseline": 3.0, "status": "anomaly"},
            "flow": {"value": 2.1, "unit": "m³/h", "baseline": 1.2, "status": "anomaly"},
            "leak_rate": {"value": 1.2, "unit": "L/min", "baseline": 0.0, "status": "anomaly"},
        },
    },
    "pipe-b1": {
        "asset_id": "pipe-b1", "name": "B栋消防管", "location": "B栋", "type": "leak",
        "metrics": {
            "pressure": {"value": 2.1, "unit": "bar", "baseline": 3.0, "status": "anomaly"},
            "leak_rate": {"value": 0.4, "unit": "L/min", "baseline": 0.0, "status": "anomaly"},
        },
    },
    "chiller-b1": {
        "asset_id": "chiller-b1", "name": "B1制冷机房冷水机组", "location": "B1制冷机房", "type": "hvac",
        "metrics": {
            "energy_kwh": {"value": 156.0, "unit": "kWh", "baseline": 120.0, "status": "anomaly"},
            "cop": {"value": 3.8, "unit": "", "baseline": 5.0, "status": "anomaly"},
            "chilled_water_temp": {"value": 9.5, "unit": "℃", "baseline": 7.0, "status": "normal"},
        },
    },
    "meter-b2": {
        "asset_id": "meter-b2", "name": "地库照明动力表", "location": "地库", "type": "lighting",
        "metrics": {
            "voltage": {"value": 198.0, "unit": "V", "baseline": 220.0, "status": "anomaly"},
            "power_kw": {"value": 8.4, "unit": "kW", "baseline": 8.0, "status": "normal"},
        },
    },
    "smoke-3f": {
        "asset_id": "smoke-3f", "name": "三楼烟感探测器", "location": "三楼", "type": "fire",
        "metrics": {
            "battery_voltage": {"value": 2.6, "unit": "V", "baseline": 3.6, "status": "anomaly"},
            "smoke_ppm": {"value": 0.02, "unit": "ppm", "baseline": 0.0, "status": "normal"},
        },
    },
}


@mcp.tool()
def read_sensor(asset_id: str, metric: str = "all") -> dict:
    """读取某资产的实时传感器遥测。metric="all" 返回全部指标，否则返回单个指标。"""
    asset = ASSETS.get(asset_id)
    if asset is None:
        return {"error": f"未知资产: {asset_id}", "known": list(ASSETS.keys())}
    metrics = asset["metrics"]
    if metric != "all":
        m = metrics.get(metric)
        if m is None:
            return {"error": f"未知指标: {metric}", "known": list(metrics.keys())}
        return {"asset_id": asset_id, "metric": metric, **m}
    return {
        "asset_id": asset_id,
        "name": asset["name"],
        "location": asset["location"],
        "metrics": metrics,
    }


@mcp.tool()
def list_assets() -> list:
    """列出所有受监控资产及其位置与类型。"""
    return [
        {"asset_id": a["asset_id"], "name": a["name"], "location": a["location"], "type": a["type"]}
        for a in ASSETS.values()
    ]


@mcp.tool()
def list_anomalies() -> list:
    """列出当前存在异常指标（status=anomaly）的资产与指标。"""
    out = []
    for a in ASSETS.values():
        bad = {k: v for k, v in a["metrics"].items() if v["status"] == "anomaly"}
        if bad:
            out.append({"asset_id": a["asset_id"], "name": a["name"], "anomalies": bad})
    return out


if __name__ == "__main__":
    mcp.run(transport="stdio")
