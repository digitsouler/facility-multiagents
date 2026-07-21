"""结构化执行追踪（Phase 6+ 可观测性）。

把一次流水线运行变成一条可读、可回溯的 trace：
- 一条 Trace = 若干顶层 Span（每个 Agent 节点一个），Span 可嵌套子 Span
  （LLM 调用 / 记忆检索 / MCP 工具调用 / QA 校验）。
- 用模块级栈维护"当前 trace / 当前 span"，子调用自动挂到父 span，无需手动传参。
  选择模块级而非 contextvars：Starlette 的 StreamingResponse 在线程池里迭代同步
  生成器，每次 next() 可能换线程 + 拷贝上下文，contextvars 会丢；模块级变量则跨
  线程/上下文稳定可见。Demo 场景顺序执行单条流水线，线程安全足够。
- 无 trace 上下文时 span() 降级为 no-op——不影响离线运行、单测、eval。

输出三路：
1. 控制台：树形彩色摘要（CLI 直接看链路）。
2. JSONL 文件：logs/trace-YYYYMMDD.jsonl，每行一条完整 trace，可回溯/分析。
3. 结构化 dict：供 Dashboard SSE 下发 + 前端渲染时间线。
"""

from __future__ import annotations

import functools
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = REPO_ROOT / "logs"

# 模块级栈：跨线程/上下文稳定，适配 CLI 同步执行 + Web 线程池迭代两种模式。
# Demo 场景顺序执行单条流水线，无需加锁；若未来支持并发需改 threading.local + 锁。
_trace_stack: list["Trace"] = []
_span_stack: list["Span"] = []

# span 类型 → 控制台颜色（ANSI）
_TYPE_COLOR = {
    "agent": "\033[36m",   # cyan
    "llm": "\033[35m",     # magenta
    "memory": "\033[33m",  # yellow
    "mcp": "\033[32m",     # green
    "qa": "\033[34m",      # blue
    "system": "\033[90m",  # gray
}
_RESET = "\033[0m"
_BOLD = "\033[1m"


@dataclass
class Span:
    name: str
    span_type: str = "step"
    started_at: float = field(default_factory=time.time)
    duration_ms: float = 0.0
    input_brief: str = ""
    output_brief: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    children: list["Span"] = field(default_factory=list)
    status: str = "ok"  # ok / error / skipped / hitl
    _parent: Optional["Span"] = None

    def finish(self, output_brief: str = "", status: str = "ok", **meta) -> None:
        self.duration_ms = round((time.time() - self.started_at) * 1000, 1)
        self.output_brief = output_brief
        self.status = status
        self.meta.update(meta)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.span_type,
            "duration_ms": self.duration_ms,
            "input": self.input_brief,
            "output": self.output_brief,
            "meta": self.meta,
            "status": self.status,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class Trace:
    ticket_id: str = ""
    started_at: float = field(default_factory=time.time)
    spans: list[Span] = field(default_factory=list)
    total_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "started_at": datetime.fromtimestamp(self.started_at).isoformat(timespec="seconds"),
            "total_ms": self.total_ms,
            "spans": [s.to_dict() for s in self.spans],
        }


def start_trace(ticket_id: str = "") -> Trace:
    """开始一条 trace，压入模块级栈顶。重复调用会叠加（支持嵌套 trace）。"""
    t = Trace(ticket_id=ticket_id)
    _trace_stack.append(t)
    _span_stack.clear()
    return t


def end_trace() -> Optional[Trace]:
    """结束当前 trace：计算总耗时、落 JSONL、弹出栈顶。无活跃 trace 返回 None。"""
    if not _trace_stack:
        return None
    t = _trace_stack.pop()
    t.total_ms = round((time.time() - t.started_at) * 1000, 1)
    _write_jsonl(t)
    _span_stack.clear()
    return t


def current_trace() -> Optional["Trace"]:
    return _trace_stack[-1] if _trace_stack else None


@contextmanager
def span(name: str, span_type: str = "step", input_brief: str = "", **meta):
    """创建一个 span 并挂到当前父 span（无 trace 上下文时 no-op）。

    用法：
        with span("LLM:deepseek", "llm", model="deepseek-chat") as s:
            ...做事...
            s.finish(output_brief="700 tokens", tokens=700)
    退出时若未手动 finish，自动 finish（status=ok）。
    """
    if not _trace_stack:
        yield None
        return
    trace = _trace_stack[-1]
    parent = _span_stack[-1] if _span_stack else None
    s = Span(name=name, span_type=span_type, input_brief=input_brief, meta=dict(meta))
    s._parent = parent
    if parent is not None:
        parent.children.append(s)
    else:
        trace.spans.append(s)
    _span_stack.append(s)
    try:
        yield s
    except Exception as exc:
        s.finish(status="error", error=f"{type(exc).__name__}: {exc}")
        raise
    else:
        if s.duration_ms == 0.0:
            s.finish()
    finally:
        _span_stack.pop()


def traced(name: str, span_type: str = "agent"):
    """装饰器：把 Agent 节点函数包成 traced span。

    仅在有活跃 trace 时生效；无 trace 时原样调用，零开销。
    """

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(state):
            if not _trace_stack:
                return fn(state)
            with span(name, span_type=span_type) as s:
                result = fn(state)
                if s is not None:
                    s.output_brief = _brief_state_output(result)
                return result

        return wrapper

    return deco


def _brief_state_output(result: dict) -> str:
    """从节点返回的 state 增量里提取一句话摘要。"""
    if not isinstance(result, dict):
        return ""
    if "diagnosis" in result:
        d = result["diagnosis"]
        return f"根因={_trunc(d.get('root_cause', ''), 30)} 置信度={d.get('confidence', 0):.2f}"
    if "dispatch_plan" in result:
        p = result["dispatch_plan"]
        return f"{p.get('vendor', '')} ¥{p.get('cost', 0):.0f}"
    if "qa" in result:
        q = result["qa"]
        return f"{'通过' if q.get('passed') else '未通过'} 得分={q.get('score', 0)}"
    if "report" in result:
        return _trunc(result["report"].get("summary", ""), 40)
    if "approval" in result:
        a = result["approval"]
        return f"[{a.get('status')}] 批准={a.get('approved')}"
    if "ticket" in result:
        t = result["ticket"]
        return f"{t.get('type')} / {t.get('urgency')}"
    return ""


def _trunc(s: str, n: int) -> str:
    s = str(s)
    return s[:n] + "…" if len(s) > n else s


# --------------------------------------------------------------------------- #
# 输出：控制台树形 + JSONL
# --------------------------------------------------------------------------- #
def _write_jsonl(trace: Trace) -> None:
    """把完整 trace 追加到 logs/trace-YYYYMMDD.jsonl。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"trace-{datetime.now().strftime('%Y%m%d')}.jsonl"
        with open(LOG_DIR / fname, "a", encoding="utf-8") as f:
            f.write(json.dumps(trace.to_dict(), ensure_ascii=False) + "\n")
    except Exception:
        pass  # 日志写入失败不影响主流程


def render_tree(trace: Trace) -> str:
    """把 trace 渲染成彩色树形文本（CLI 打印用）。"""
    lines = [
        f"{_BOLD}📋 Trace · {trace.ticket_id} · {trace.total_ms:.0f}ms{_RESET}"
    ]
    for i, s in enumerate(trace.spans):
        _render_span(s, "", i == len(trace.spans) - 1, lines)
    return "\n".join(lines)


def _render_span(s: Span, prefix: str, is_last: bool, lines: list[str]) -> None:
    connector = "└─ " if is_last else "├─ "
    color = _TYPE_COLOR.get(s.span_type, "")
    dur = f"{s.duration_ms:.0f}ms"
    status_icon = {"ok": "✓", "error": "✗", "skipped": "○", "hitl": "⏸"}.get(s.status, "·")
    meta_str = ""
    if s.meta:
        parts = [f"{k}={v}" for k, v in s.meta.items() if k != "error"]
        if s.status == "error" and "error" in s.meta:
            parts.append(f"err={s.meta['error']}")
        meta_str = "  " + " ".join(parts[:4]) if parts else ""
    out = f"  {s.output_brief}" if s.output_brief else ""
    lines.append(
        f"{prefix}{connector}{color}{s.name:<28}{_RESET} {dur:>7}  {status_icon}{out}{meta_str}"
    )
    child_prefix = prefix + ("   " if is_last else "│  ")
    for j, c in enumerate(s.children):
        _render_span(c, child_prefix, j == len(c.children) - 1, lines)
