"""Diagnose Agent：用 ReAct 子图（agent → ToolNode → should_continue）做根因诊断。

工具来自 tools 注册中心（lookup_kb / read_sensor / recall_cases），经 ToolNode 执行；
在线模式由 LLM 决定每步调用哪个工具，离线模式走脚本化序列，二者共用同一套 @tool。
循环靠 should_continue（无 tool_calls 即终止）+ MAX_ITER 控制，不会无限转圈。
"""

import json
import logging
import time
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from ..dataio import load_tickets
from ..knowledge import KB, TYPE_KEYWORDS
from ..llm import extract_json, get_agent_client
from ..state import Diagnosis, FacilityState
from ..tools import get_tools_for_agent

log = logging.getLogger("facilitymind.diagnose")

MAX_ITER = 4

# ReAct 诊断必须按此顺序执行：先取证，再查知识，再召回案例
TOOL_ORDER = ["read_sensor", "lookup_kb", "recall_cases"]


class _DiagState(TypedDict):
    messages: Annotated[list, add_messages]
    ticket: dict
    iteration: int


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


def _offline_ai(msgs: list, ticket: dict, iteration: int) -> AIMessage:
    """离线脚本化决策：严格按 TOOL_ORDER 一次只推进一个未调用工具，全部执行完再收尾。"""
    ttype = ticket["type"]
    called = {m.name for m in msgs if isinstance(m, ToolMessage)}
    if iteration >= MAX_ITER or all(t in called for t in TOOL_ORDER):
        return AIMessage(content=_offline_finish(ticket, msgs))
    for name in TOOL_ORDER:
        if name not in called:
            args = {"fault_type": ttype}
            if name == "recall_cases":
                args["location"] = ticket.get("location", "")
            log.info("[Diagnose][ReAct] 决定(离线) 调 %s(%s)", name, args)
            return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}"}])
    return AIMessage(content=_offline_finish(ticket, msgs))


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


def _build_diag_graph(tools: list):
    """编译 ReAct 子图：agent 决定下一步（tool_calls 或最终诊断），ToolNode 执行工具。"""
    tool_node = ToolNode(tools)
    schemas = _to_schemas(tools)

    def _called_tools(msgs: list) -> set:
        return {m.name for m in msgs if isinstance(m, ToolMessage)}

    def _pick_one_call(tool_calls: list, called: set, ticket: dict) -> tuple[str, dict] | None:
        """LLM 可能一次返回多个工具调用，这里过滤已调用过的，并强制按 TOOL_ORDER 取第一个未调用的。"""
        valid = {tc["name"]: tc["args"] for tc in tool_calls if tc["name"] in TOOL_ORDER}
        for name in TOOL_ORDER:
            if name in valid and name not in called:
                args = dict(valid[name])
                args["fault_type"] = ticket["type"]  # 防止 LLM 传错类型
                if name == "recall_cases":
                    args["location"] = ticket.get("location", "")
                return name, args
        return None

    def agent_node(state: _DiagState):
        client = get_agent_client("diagnose")
        ticket = state["ticket"]
        msgs = state["messages"]
        iteration = state.get("iteration", 0) + 1
        ttype = ticket["type"]
        called = _called_tools(msgs)

        sys_prompt = (
            "你是设施管理诊断专家，用 ReAct 工作。\n"
            "可用工具：read_sensor(fault_type)、lookup_kb(fault_type)、recall_cases(fault_type, location)。\n"
            f"当前工单标准化故障类型是 '{ttype}'，工具参数 fault_type 必须用它。\n"
            "严格一次只调用一个工具，按顺序执行：1) read_sensor 先读现场传感器取证；2) lookup_kb 查知识库；3) recall_cases 召回历史案例；4) 给出 JSON 诊断。\n"
            "如果已拿到传感器、知识库、历史案例中的部分信息，就继续下一步，不要重复调用已经调过的工具。\n"
            '最终以 JSON 返回 {"root_cause":"...","recommended_action":"...","confidence":0.0~1.0}。'
        )

        if iteration >= MAX_ITER or all(t in called for t in TOOL_ORDER):
            log.info("[Diagnose][ReAct] 已达最大迭代或工具已全部调用，强制收尾")
            content = _offline_finish(ticket, msgs)
            return {"messages": [AIMessage(content=content)], "iteration": iteration}

        if client and client.available:
            out = client.complete_with_tools(sys_prompt, _diag_user(ticket, msgs), schemas)
            tool_calls = out.get("tool_calls", [])
            log.info("[Diagnose][ReAct] LLM 原始决策=%s", tool_calls or "finish")
            if tool_calls:
                picked = _pick_one_call(tool_calls, called, ticket)
                if picked:
                    name, args = picked
                    log.info("[Diagnose][ReAct] 强制顺序 调 %s(%s)", name, args)
                    ai = AIMessage(
                        content=out.get("content", ""),
                        tool_calls=[{"name": name, "args": args, "id": f"call_{name}"}],
                    )
                    return {"messages": [ai], "iteration": iteration}
            # LLM 没返回工具调用，或返回的都是已调用过的，直接用其 content 收尾
            content = out.get("content") or _offline_finish(ticket, msgs)
            return {"messages": [AIMessage(content=content)], "iteration": iteration}

        ai = _offline_ai(msgs, ticket, iteration)
        return {"messages": [ai], "iteration": iteration}

    def should_continue(state: _DiagState) -> str:
        if state.get("iteration", 0) >= MAX_ITER:
            return END
        if getattr(state["messages"][-1], "tool_calls", None):
            return "tools"
        return END

    g = StateGraph(_DiagState)
    g.add_node("agent", agent_node)
    g.add_node("tools", tool_node)
    g.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    g.add_edge("tools", "agent")
    g.set_entry_point("agent")
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

    init = {"ticket": ticket, "messages": [HumanMessage(content=_diag_user(ticket, []))], "iteration": 0}
    result = _DIAG_GRAPH.invoke(init)
    # log.info("[Diagnose] ▶ 调用(工具) result=%s", result)

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
