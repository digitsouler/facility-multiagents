"""Diagnose Agent：hybrid 诊断（并行取证 + 条件 ReAct）。

- 固定的三类取证（read_sensor / lookup_kb / recall_cases）彼此独立、无依赖，用
  LangGraph `Send()` 一次性并行扇出，再由 `synthesize` 做一次综合诊断（省掉原本
  每步一次的 LLM 决策调用，token 明显下降）。
- 仅在综合诊断置信度偏低（< CONF_THRESHOLD）时，才进入 `probe` 自适应 ReAct 子循环
  （最多 PROBE_MAX 轮）补充取证/重新推理；其余情况直接出诊断。
- 在线无 Key 时走规则兜底（_offline_finish），并行取证的三类工具本身不依赖 LLM。
"""

import json
import logging
import time
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, Send

from ..dataio import load_tickets
from ..knowledge import KB, TYPE_KEYWORDS
from ..llm import extract_json, get_agent_client
from ..state import Diagnosis, FacilityState
from ..tools import get_tools_for_agent

log = logging.getLogger("facilitymind.diagnose")

# 综合诊断置信度低于此值才进 probe 自适应 ReAct；其余直接出诊断
CONF_THRESHOLD = 0.75
# 自适应 ReAct 最大轮数（含工具调用 + 重综合）
PROBE_MAX = 2

_SYNTH_SYS = (
    "你是设施管理诊断专家。已为你准备好三类证据：IoT 传感器实时读数、故障知识库、"
    "历史相似案例。请综合判断根因并给出处置建议。\n"
    '以 JSON 返回 {"root_cause":"...","recommended_action":"...","confidence":0.0~1.0}。\n'
    "若证据矛盾（如传感器显示正常但业主报修异常），请在根因中说明并给出合理推断。"
)

_PROBE_SYS = (
    "你是设施管理诊断专家。上一轮综合诊断置信度偏低，需要重新审查证据。\n"
    "你可继续调用工具补充取证（read_sensor/lookup_kb/recall_cases），然后重新给出 JSON 诊断：\n"
    '{"root_cause":"...","recommended_action":"...","confidence":0.0~1.0}。'
)


class _DiagState(TypedDict):
    messages: Annotated[list, add_messages]
    ticket: dict
    confidence: float
    probe_iteration: int


def _check_recurrence(ticket) -> bool:
    """同一位置同类故障若在当前工单之前已出现，视为重复发生。"""
    history = load_tickets()
    try:
        cur = int(str(ticket["id"]).split("-")[-1])
    except (ValueError, KeyError):
        return False
    location = ticket.get("location", "")
    if not location or location == "未指定位置":
        return False
    earlier = [
        t for t in history
        if t.get("type") == ticket["type"]
        and int(str(t["id"]).split("-")[-1]) < cur
        and location in t.get("location_hint", "")
    ]
    return len(earlier) >= 1


def _to_schemas(tools: list) -> list:
    """把 @tool 转成 OpenAI function-calling schema（供 LLM 选择工具）。"""
    out = []
    for t in tools:
        try:
            out.append(t.to_openai())
            continue
        except Exception:
            pass
        try:
            from langchain_core.utils.function_calling import convert_to_openai_tool

            out.append(convert_to_openai_tool(t))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"无法把工具 {t.name} 转成 OpenAI schema: {exc}") from exc
    return out


def _diag_user(ticket: dict, msgs: list) -> str:
    obs = [f"{m.name}={m.content}" for m in msgs if isinstance(m, ToolMessage)]
    obs_text = "; ".join(obs) or "无"
    return (
        f"工单原文：{ticket['raw']}\n"
        f"标准化故障类型：{ticket['type']}\n"
        f"已观察：{obs_text}"
    )


def _tool_content(msgs: list, name: str) -> dict:
    """取某工具最近一次返回的 JSON（dict），取不到返回空 dict。"""
    for m in reversed(msgs):
        if isinstance(m, ToolMessage) and m.name == name:
            try:
                return json.loads(m.content)
            except (json.JSONDecodeError, TypeError):
                return {}
    return {}


def _offline_finish(ticket: dict, msgs: list) -> str:
    ttype = ticket["type"]
    kb = dict(KB.get(ttype, KB["cleaning"]))
    note = _tool_content(msgs, "read_sensor").get("note", "")
    root_cause = f"{kb['root_cause']}（{note}）" if note else kb["root_cause"]
    return json.dumps(
        {
            "root_cause": root_cause,
            "recommended_action": kb["recommended_action"],
            "confidence": 0.9 if _check_recurrence(ticket) else 0.82,
        },
        ensure_ascii=False,
    )


def _probe_args(tc: dict, ticket: dict) -> dict:
    """修正 LLM 可能传错的参数：强制 fault_type 用标准 key，recall_cases 补 location。"""
    args = dict(tc.get("args", {}))
    args["fault_type"] = ticket["type"]
    if tc["name"] == "recall_cases":
        args["location"] = ticket.get("location", "")
    return args


def _truncate(text: str, n: int = 220) -> str:
    """日志截断，避免长 JSON 撑爆一行。"""
    if len(text) <= n:
        return text
    return text[: n - 3] + "..."


def _build_diag_graph(tools: list):
    """编译 hybrid 子图：并行取证扇出 → 单次综合诊断 → 低置信度进自适应 ReAct。"""
    tool_map = {t.name: t for t in tools}
    tool_node = ToolNode(tools)
    schemas = _to_schemas(tools)

    def _gather(name: str, args: dict, call_id: str) -> ToolMessage:
        out = tool_map[name].invoke(args)
        return ToolMessage(content=out, name=name, tool_call_id=call_id)

    def fan_out(state: _DiagState) -> Command:
        # 三个取证工具相互独立，并行扇出（LangGraph 同一 super-step 内并发执行）
        return Command(goto=[
            Send("gather_sensor", state),
            Send("gather_kb", state),
            Send("gather_cases", state),
        ])

    def gather_sensor(state: _DiagState):
        return {"messages": [_gather("read_sensor", {"fault_type": state["ticket"]["type"]}, "sensor")]}

    def gather_kb(state: _DiagState):
        return {"messages": [_gather("lookup_kb", {"fault_type": state["ticket"]["type"]}, "kb")]}

    def gather_cases(state: _DiagState):
        loc = state["ticket"].get("location", "")
        return {"messages": [_gather("recall_cases", {"fault_type": state["ticket"]["type"], "location": loc}, "cases")]}

    def synthesize(state: _DiagState):
        ticket = state["ticket"]
        client = get_agent_client("diagnose")
        if client and client.available:
            content = client.complete(_SYNTH_SYS, _diag_user(ticket, state["messages"]))
            parsed = extract_json(content) or {}
            conf = float(parsed.get("confidence", 0.82))
        else:
            content = _offline_finish(ticket, state["messages"])
            conf = 0.9 if _check_recurrence(ticket) else 0.82
        log.info("[Diagnose][hybrid] 综合诊断 置信度=%.2f", conf)
        return {"messages": [AIMessage(content=content)], "confidence": conf}

    def route_after_synth(state: _DiagState) -> str:
        if state.get("probe_iteration", 0) >= PROBE_MAX:
            return END
        if state.get("confidence", 1.0) >= CONF_THRESHOLD:
            return END
        return "probe"

    def probe(state: _DiagState):
        client = get_agent_client("diagnose")
        ticket = state["ticket"]
        msgs = state["messages"]
        probe_iter = state.get("probe_iteration", 0) + 1
        log.info("[Diagnose][probe][round %d] ▶ 进入 ReAct 循环", probe_iter)
        if client and client.available:
            out = client.complete_with_tools(_PROBE_SYS, _diag_user(ticket, msgs), schemas)
            thought = (out.get("content") or "").strip()
            if thought:
                log.info("[Diagnose][probe][round %d] Thought: %s", probe_iter, _truncate(thought))
            tool_calls = out.get("tool_calls", [])
            if tool_calls and probe_iter < PROBE_MAX:
                for tc in tool_calls:
                    log.info(
                        "[Diagnose][probe][round %d] Action: %s(%s)",
                        probe_iter, tc["name"], json.dumps(_probe_args(tc, ticket), ensure_ascii=False),
                    )
                ai = AIMessage(
                    content=out.get("content", ""),
                    tool_calls=[{"name": tc["name"], "args": _probe_args(tc, ticket), "id": f"probe_{tc['name']}"}
                                for tc in tool_calls],
                )
                return {"messages": [ai], "probe_iteration": probe_iter}
            # 不再调工具或已达上限 → 重新综合
            log.info("[Diagnose][probe][round %d] 不再调用工具，直接重新综合诊断", probe_iter)
            content = client.complete(_SYNTH_SYS, _diag_user(ticket, msgs))
            parsed = extract_json(content) or {}
            conf = float(parsed.get("confidence", 0.6))
            log.info("[Diagnose][probe][round %d] Final synthesis 置信度=%.2f", probe_iter, conf)
            return {"messages": [AIMessage(content=content)], "confidence": conf, "probe_iteration": probe_iter}
        # 离线：直接规则收尾
        log.info("[Diagnose][probe][round %d] 离线模式，直接规则收尾", probe_iter)
        content = _offline_finish(ticket, msgs)
        return {"messages": [AIMessage(content=content)], "confidence": 0.82, "probe_iteration": probe_iter}

    def observe(state: _DiagState):
        """tools 节点执行后，打印每条工具的 Observation，体现 ReAct 的观察步骤。"""
        probe_iter = state.get("probe_iteration", 1)
        # 找到最近一次带 tool_calls 的 AI 消息，其后紧跟的就是这次行动的 Observation
        ai_idx = None
        for i in range(len(state["messages"]) - 1, -1, -1):
            m = state["messages"][i]
            if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
                ai_idx = i
                break
        if ai_idx is None:
            return {}
        tool_call_ids = {tc["id"] for tc in state["messages"][ai_idx].tool_calls}
        for m in state["messages"][ai_idx + 1 :]:
            if isinstance(m, ToolMessage) and m.tool_call_id in tool_call_ids:
                log.info(
                    "[Diagnose][probe][round %d] Observation: %s = %s",
                    probe_iter, m.name, _truncate(m.content),
                )
        return {}

    def _probe_should_continue(state: _DiagState) -> str:
        if getattr(state["messages"][-1], "tool_calls", None):
            return "tools"
        return END

    g = StateGraph(_DiagState)
    g.add_node("fan_out", fan_out)
    g.add_node("gather_sensor", gather_sensor)
    g.add_node("gather_kb", gather_kb)
    g.add_node("gather_cases", gather_cases)
    g.add_node("synthesize", synthesize)
    g.add_node("probe", probe)
    g.add_node("tools", tool_node)
    g.add_node("observe", observe)

    g.add_edge(START, "fan_out")
    g.add_edge("gather_sensor", "synthesize")
    g.add_edge("gather_kb", "synthesize")
    g.add_edge("gather_cases", "synthesize")
    g.add_conditional_edges("synthesize", route_after_synth, {"probe": "probe", END: END})
    g.add_conditional_edges("probe", _probe_should_continue, {"tools": "tools", END: END})
    g.add_edge("tools", "observe")
    g.add_edge("observe", "probe")
    return g.compile()


_DIAG_GRAPH = _build_diag_graph(get_tools_for_agent("diagnose"))


def _parse_diagnosis(ticket: dict, content: str, msgs: list) -> Diagnosis:
    parsed = extract_json(content) or {}
    ttype = ticket["type"]
    kb = dict(KB.get(ttype, KB["cleaning"]))
    note = _tool_content(msgs, "read_sensor").get("note", "")
    root_cause = parsed.get("root_cause") or kb["root_cause"]
    if note and root_cause == kb["root_cause"]:
        root_cause = f"{root_cause}（{note}）"
    return {
        "root_cause": root_cause,
        "recommended_action": parsed.get("recommended_action") or kb["recommended_action"],
        "required_skill": kb["required_skill"],
        "estimated_cost": kb["estimated_cost"],
        "sla_hours": kb["sla_hours"],
        "confidence": float(parsed.get("confidence", 0.9 if _check_recurrence(ticket) else 0.82)),
        "recurrence": _check_recurrence(ticket),
    }


def diagnose_agent(state: FacilityState) -> dict:
    ticket = state["ticket"]
    start = time.time()
    log.info("[Diagnose] ▶ 开始 ticket=%s 类型=%s", ticket["id"], ticket["type"])

    init = {
        "ticket": ticket,
        "messages": [HumanMessage(content=_diag_user(ticket, []))],
        "confidence": 1.0,
        "probe_iteration": 0,
    }
    result = _DIAG_GRAPH.invoke(init)

    diag = _parse_diagnosis(ticket, result["messages"][-1].content, result["messages"])

    # 新问题查证：KB 兜底的未知类型（type=cleaning 但原文不含保洁关键词）→ 并行查案例库+口碑给候选
    looks_unknown = (ticket["type"] == "cleaning") and not any(
        k in ticket["raw"] for k in TYPE_KEYWORDS["cleaning"]
    )
    if looks_unknown or diag.get("confidence", 1.0) < 0.5:
        from ..memory.qdrant_cases import verify_new_issue

        skill = KB.get(ticket["type"], {}).get("required_skill", "cleaning")
        verify = verify_new_issue(ticket["raw"], ticket["type"], skill)
        diag["verify"] = verify
        log.info(
            "[Diagnose][Verify] 新问题查证 → 案例=%d 候选商=%d 需人工=%s",
            len(verify["similar_cases"]),
            len(verify["candidate_vendors"]),
            verify["needs_human"],
        )

    log.info(
        "[Diagnose] ✔ 完成 用时=%.2fs 根因=%s；建议=%s；预估¥%.0f；SLA %sh；置信度=%.2f",
        time.time() - start, diag["root_cause"], diag["recommended_action"],
        diag["estimated_cost"], diag["sla_hours"], diag["confidence"],
    )
    return {
        "diagnosis": diag,
        "messages": [{"role": "system",
                      "content": f"[Diagnose] 类型={ticket['type']} → 根因={diag['root_cause']}；建议={diag['recommended_action']}；置信度={diag['confidence']:.2f}"}],
    }
