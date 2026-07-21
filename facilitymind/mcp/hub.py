"""MCPHub：按 mcp.json 拉起 MCP server 子进程，提供同步工具调用。

设计要点：
- 每个 enabled server 在独立后台线程 + 独立事件循环中保持长连接
  （stdio 子进程常驻），call_tool 通过 run_coroutine_threadsafe 调度，
  避免在 uvicorn 已运行的事件循环里调用 asyncio.run 导致冲突。
- server 起不来 / 工具不可用 -> available=False，Agent 侧自动回退 KB，
  整条流水线不中断。
- 单 server 初始化失败不会拖垮整个 hub。

调用示例（在 Agent 内）：
    from ..mcp import get_hub
    hub = get_hub()
    if hub.available("iot"):
        data = hub.call_tool("iot", "read_sensor", {"asset_id": "ahu-12f"})
"""
import asyncio
import json
import threading

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import load_config, resolve_command


class _ServerConn:
    """单个 MCP server 的长连接封装（后台线程 + 独立事件循环）。"""

    def __init__(self, spec: dict):
        self.spec = spec
        self.name = spec["name"]
        self.loop = None
        self.session = None
        self.available = False
        self.error = None
        self._stop = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"mcp-{self.name}"
        )
        self._thread.start()
        # 等连接建立或失败；超时即视为不可用（不阻塞主流程）
        self._ready.wait(timeout=20)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._connect())

    async def _connect(self):
        command, args = resolve_command(self.spec)
        params = StdioServerParameters(
            command=command, args=args, env=self.spec.get("env") or None
        )
        try:
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    self.session = session
                    self.available = True
                    self._ready.set()
                    self._stop = asyncio.Event()
                    await self._stop.wait()  # 常驻，直到 shutdown
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            self.available = False
            self._ready.set()

    def call_tool(self, name: str, args: dict, timeout: int = 10):
        if not self.available or self.session is None:
            return None
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._acall(name, args), self.loop
            )
            return fut.result(timeout=timeout)
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}

    async def _acall(self, name: str, args: dict):
        res = await self.session.call_tool(name, args or {})
        out = []
        for c in res.content:
            t = getattr(c, "text", None)
            if t:
                try:
                    out.append(json.loads(t))
                except Exception:
                    out.append(t)
        return out[0] if len(out) == 1 else out

    def list_tools(self, timeout: int = 10):
        if not self.available:
            return []
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self.session.list_tools(), self.loop
            )
            tools = fut.result(timeout=timeout)
            return [t.name for t in tools.tools]
        except Exception:
            return []

    def shutdown(self):
        if self._stop is not None and self.loop is not None:
            self.loop.call_soon_threadsafe(self._stop.set)


class MCPHub:
    """进程内 MCP server 注册中心。"""

    def __init__(self, config: dict | None = None):
        self._conns: dict[str, _ServerConn] = {}
        cfg = config or load_config()
        for spec in cfg.get("servers", []):
            if not spec.get("enabled", True):
                continue
            try:
                self._conns[spec["name"]] = _ServerConn(spec)
            except Exception:
                # 单个 server 初始化失败不应拖垮整个 hub
                continue

    def available(self, name: str) -> bool:
        c = self._conns.get(name)
        return bool(c and c.available)

    def servers(self) -> dict:
        return {
            name: {
                "available": c.available,
                "tools": c.list_tools(),
                "error": c.error,
            }
            for name, c in self._conns.items()
        }

    def call_tool(self, server: str, name: str, args: dict | None = None, timeout: int = 10):
        c = self._conns.get(server)
        if not c:
            return {"error": f"未配置的 MCP server: {server}"}
        return c.call_tool(name, args, timeout=timeout)

    def list_tools(self, server: str):
        c = self._conns.get(server)
        return c.list_tools() if c else []

    def shutdown(self):
        for c in self._conns.values():
            c.shutdown()


_hub: MCPHub | None = None


def get_hub() -> MCPHub:
    """获取进程内 MCPHub 单例（首次调用时按 mcp.json 建立连接）。"""
    global _hub
    if _hub is None:
        _hub = MCPHub()
    return _hub
