"""MCP 薄客户端：把本地 MCP server 的 read_sensor 转成普通同步函数。

- 通过 stdio 启动 facilitymind.mcp.server 子进程并调用，真实走 MCP 协议；
- 任意失败（未装 mcp / server 异常）优雅回退到桩数据，保证离线可跑；
- 返回 dict 带 source 字段（mcp/stub），调用方据此记日志区分。
"""

import asyncio
import json
import logging
import os
import sys

log = logging.getLogger("facilitymind.mcp.client")

# 项目根（facilitymind/ 这一层），确保子进程能 `python -m facilitymind.mcp.server`
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(fault_type: str) -> list[dict]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "facilitymind.mcp.server"],
        env={**os.environ, "PYTHONPATH": ROOT, "PYTHONUNBUFFERED": "1"},
        cwd=ROOT,
    )
    async def _call():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                res = await sess.call_tool("read_sensor", {"fault_type": fault_type})
                return json.loads(res.content[0].text)["rows"]

    return asyncio.run(_call())


def read_iot_sensor(fault_type: str) -> dict:
    """经 MCP 读取传感器；失败回退桩。返回 {source, fault_type, rows}。"""
    try:
        rows = _read(fault_type)
        return {"source": "mcp", "fault_type": fault_type, "rows": rows}
    except Exception as exc:  # noqa: BLE001
        log.warning("[MCP] read_sensor 失败，回退桩：%s", exc)
        return {
            "source": "stub",
            "fault_type": fault_type,
            "rows": [{"asset": "桩", "note": "MCP 不可用，使用模拟读数", "status": "未知"}],
        }
