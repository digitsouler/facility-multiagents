"""FastAPI 服务层：把 FacilityMind 引擎暴露成 Web Dashboard 后端。

设计要点：
- 不重写任何 Agent：直接复用编译好的 LangGraph 图（graph.app）；
- 实时流水线：engine.stream(stream_mode="updates") 逐节点产出，经 SSE 推给前端点亮；
- M3 网页人工确认：auto=0 时 Approval 触发 interrupt，后端以 SSE 推送 interrupt 事件并阻塞
  等待 /api/approve 的网页决策，再用 Command(resume=...) 续跑（真正的浏览器内 HITL）；
- M4 评估报告：/api/eval 复用 eval.py 的 run_one/aggregate，把量化指标交给前端画图；
- 存储：用进程内内存（RUNS），重启即丢；后续可换 SqliteSaver 持久化。
"""

import json
import os
import threading
import time
from typing import Iterator

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from pydantic import BaseModel

from ..dataio import load_tickets
from ..eval import aggregate, run_one
from ..graph import app as engine
from ..llm import calls_breakdown, list_models, usage_breakdown
from ..memory import get_store
from ..tracer import end_trace, start_trace

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# 节点中文标签，与流水线顺序一致
NODE_ORDER = ["intake", "diagnose", "dispatch", "approval", "qa", "report"]
NODE_LABELS = {
    "intake": "受理",
    "diagnose": "诊断",
    "dispatch": "派单",
    "approval": "人工确认",
    "qa": "质检",
    "report": "报告",
}
# 故障类型中文名
TYPE_CN = {
    "elevator": "电梯",
    "hvac": "暖通空调",
    "leak": "漏水",
    "lighting": "照明",
    "fire": "消防",
    "access": "门禁道闸",
    "cleaning": "保洁",
    "greening": "绿化",
}

# 进程内内存存储：thread_id -> 最终归并状态（去掉 messages）
RUNS: dict[str, dict] = {}
# thread_id -> 本单的 token 计量（按模型拆分），与 RUNS 配套展示
RUN_META: dict[str, dict] = {}

# M3 网页人工确认所需的等待队列：ticket_id -> threading.Event
PENDING: dict[str, threading.Event] = {}
# ticket_id -> 决策字典（由 /api/approve 写入）
DECISIONS: dict[str, dict] = {}
# 等待决策的超时（秒）：超时视为流程终止
APPROVE_TIMEOUT = 300

api = FastAPI(title="FacilityMind Dashboard", version="0.2.0")


def _sse(event: str, data: dict) -> str:
    """构造一条 SSE 消息。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _clean(state: dict) -> dict:
    """去掉不便 JSON 序列化 / 前端用不到的字段（messages 内含对象）。"""
    return {k: v for k, v in (state or {}).items() if k != "messages"}


def _tickets_index() -> dict[str, dict]:
    return {t["id"]: t for t in load_tickets()}


def _wait_decision(ticket_id: str) -> dict | None:
    """阻塞等待网页端对工单的人工决策；超时返回 None（调用方据此终止流程）。"""
    ev = threading.Event()
    PENDING[ticket_id] = ev
    try:
        if not ev.wait(APPROVE_TIMEOUT):
            return None
        return DECISIONS.pop(ticket_id, {"approved": False, "note": "超时默认驳回", "approver": "system"})
    finally:
        PENDING.pop(ticket_id, None)


@api.get("/api/tickets")
def api_tickets():
    """工单看板数据：原始工单 + 类型中文名 + 是否已有运行结果。"""
    out = []
    for t in load_tickets():
        item = dict(t)
        item["type_cn"] = TYPE_CN.get(t.get("type"), t.get("type"))
        item["has_result"] = t["id"] in RUNS
        out.append(item)
    return out


@api.get("/api/result/{ticket_id}")
def api_result(ticket_id: str):
    """返回内存中某工单最近一次运行的完整结果（含按模型的 token 计量）。"""
    if ticket_id not in RUNS:
        return JSONResponse({"error": "尚无运行结果"}, status_code=404)
    data = dict(RUNS[ticket_id])
    data.update(RUN_META.get(ticket_id, {}))  # 合并 tokens / model_tokens / llm_calls
    return data


@api.get("/api/models")
def api_models():
    """返回已声明模型清单（name / label / available），供前端展示友好名称。"""
    return {"models": list_models()}


@api.get("/api/mcp")
def api_mcp():
    """返回 MCP server 接入状态（供 Dashboard 展示 IoT / CMMS 等在线情况）。"""
    from ..mcp import get_hub
    try:
        servers = get_hub().servers()
    except Exception as e:  # noqa: BLE001
        servers = {"_error": {"available": False, "error": str(e)}}
    return {"servers": servers}


class ApprovalDecision(BaseModel):
    ticket_id: str
    approved: bool
    note: str = ""


@api.post("/api/approve")
def api_approve(decision: ApprovalDecision):
    """网页端对处于 interrupt 状态的工单做人工决策；唤醒阻塞中的流水线。"""
    if decision.ticket_id not in PENDING:
        return JSONResponse(
            {"error": "当前没有待审批的工单，或审批已超时"}, status_code=400
        )
    DECISIONS[decision.ticket_id] = {
        "approved": decision.approved,
        "note": decision.note,
        "approver": "网页审批人",
    }
    PENDING[decision.ticket_id].set()
    return {"ok": True}


@api.get("/api/eval")
def api_eval():
    """M4 评估报告接口：批量跑内置工单，返回聚合指标 + 逐工单明细。"""
    tickets = load_tickets()
    records = [run_one(t) for t in tickets]
    metrics = aggregate(records)
    return {"metrics": metrics, "records": records}


@api.get("/api/memory")
def api_memory():
    """记忆层看板：统计 + 近期事件记忆 + 沉淀知识。记忆不可用时安全降级。"""
    store = get_store()
    if store.disabled:
        return {"disabled": True, "stats": store.stats()}
    return {
        "disabled": False,
        "stats": store.stats(),
        "recent": store.get_incidents(limit=12),
        "kb": store.get_kb_learnings(limit=12),
    }


@api.post("/api/memory/maintain")
def api_memory_maintain():
    """记忆维护：沉淀（QA 有效处置→长期知识）+ 归档（超期事件记忆）。"""
    from ..memory.decay import run_maintenance
    return run_maintenance()


def _run_stream(ticket: dict, auto_approve: bool, pace: float, ensemble: bool = False) -> Iterator[str]:
    """逐节点运行流水线并以 SSE 事件产出；auto_approve=False 时支持 M3 网页人工确认。"""
    config = {"configurable": {"thread_id": ticket["id"]}}
    initial = {"ticket": ticket, "auto_approve": auto_approve, "ensemble": ensemble}

    # 跑之前快照各模型累计用量；结束后做差得到"本单"消耗（注册表为进程级全局累加）。
    # 并发跑多条流水线时差值可能互串，Demo 场景为单流顺序执行，足够准确。
    _before_tok = usage_breakdown()
    _before_call = calls_breakdown()
    # 记忆库快照：结束后做差得出"本单写入"的记忆条数（事件/资产/沉淀知识）。
    _before_mem = get_store().stats()
    # 结构化 trace：一次运行 = 一条 trace，各 Agent/LLM/记忆/MCP 自动埋点。
    start_trace(ticket["id"])

    yield _sse("start", {"ticket_id": ticket["id"], "order": NODE_ORDER})

    try:
        # 第一阶段：跑到第一个中断点（若有）
        for chunk in engine.stream(initial, config, stream_mode="updates"):
            for node, payload in chunk.items():
                if node == "__interrupt__":
                    intr = payload[0]
                    value = getattr(intr, "value", {})
                    yield _sse("interrupt", {"ticket_id": ticket["id"], "value": value})

                    decision = _wait_decision(ticket["id"])
                    if decision is None:
                        yield _sse("error", {"message": "人工确认超时，流程已终止"})
                        return

                    # 第二阶段：恢复执行（approval → qa → report）
                    for chunk2 in engine.stream(
                        Command(resume=decision), config, stream_mode="updates"
                    ):
                        for node2, payload2 in chunk2.items():
                            if node2 == "__interrupt__":
                                continue
                            data = _clean(payload2)
                            yield _sse(
                                "node",
                                {"node": node2, "label": NODE_LABELS.get(node2, node2), "data": data},
                            )
                            if pace > 0:
                                time.sleep(pace)
                else:
                    data = _clean(payload)
                    yield _sse(
                        "node",
                        {"node": node, "label": NODE_LABELS.get(node, node), "data": data},
                    )
                    if pace > 0:
                        time.sleep(pace)

        # 取最终归并状态存内存，供看板复看
        final_state = _clean(engine.get_state(config).values)
        RUNS[ticket["id"]] = final_state
        # 结束 trace：计算总耗时、落 JSONL、返回结构化 dict
        _trace = end_trace()
        _trace_dict = _trace.to_dict() if _trace else None

        # 计算本单各模型 token / 调用消耗（做差）
        _after_tok = usage_breakdown()
        _after_call = calls_breakdown()
        models = sorted(set(_before_tok) | set(_after_tok))
        model_tokens = {m: max(0, _after_tok.get(m, 0) - _before_tok.get(m, 0)) for m in models}
        model_calls = {m: max(0, _after_call.get(m, 0) - _before_call.get(m, 0)) for m in models}
        tokens = sum(model_tokens.values())
        calls = sum(model_calls.values())
        RUN_META[ticket["id"]] = {
            "tokens": tokens,
            "model_tokens": model_tokens,
            "model_calls": model_calls,
            "llm_calls": calls,
            "trace": _trace_dict,
        }
        # 记忆写入差量（本单新沉淀的事件记忆/资产档案/沉淀知识）
        _after_mem = get_store().stats()
        memory_written = {
            "incidents": max(0, _after_mem.get("incidents", 0) - _before_mem.get("incidents", 0)),
            "asset_knowledge": max(0, _after_mem.get("asset_knowledge", 0) - _before_mem.get("asset_knowledge", 0)),
            "kb": max(0, _after_mem.get("kb_learnings", 0) - _before_mem.get("kb_learnings", 0)),
        }
        yield _sse(
            "done",
            {
                "ticket_id": ticket["id"],
                "result": final_state,
                "tokens": tokens,
                "model_tokens": model_tokens,
                "llm_calls": calls,
                "memory_written": memory_written,
                "trace": _trace_dict,
            },
        )
    except Exception as exc:  # 兜底：任何异常都通过 SSE 反馈前端，避免连接静默中断
        end_trace()  # 清理 trace 上下文，避免 contextvar 泄漏
        yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})


@api.get("/api/stream")
def api_stream(id: str, auto: int = 1, pace: float = 0.5, ensemble: int = 0):
    """SSE 端点：实时推送某工单的流水线执行过程。

    auto=1（默认）：成本超阈值也自动放行，跑通完整流水线（M1/M2 行为）。
    auto=0：成本超阈值时触发 interrupt，需网页端 /api/approve 决策后继续（M3 行为）。
    ensemble=1：诊断阶段启用多模型集成（Ensemble）。
    """
    tickets = _tickets_index()
    if id not in tickets:
        return JSONResponse({"error": f"未找到工单 {id}"}, status_code=404)
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(
        _run_stream(tickets[id], bool(auto), max(0.0, pace), bool(ensemble)),
        media_type="text/event-stream",
        headers=headers,
    )


@api.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# 静态资源挂载在最后，避免覆盖上面的 API 路由
api.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
