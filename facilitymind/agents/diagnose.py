"""Diagnose Agent：根因诊断与处置建议。

职责：结合故障类型知识库，给出根因、建议动作、所需技能、预估成本、SLA。
有 LLM 时让模型做推理；无 LLM 时回退到知识库确定性结论。
（经验记忆层后续基于 Redis / Qdrant 重建；ReAct 自主循环、多模型 Ensemble、IoT 证据化诊断
均为后续可选增强，当前走确定性 + 单轮单模型 LLM 路径。）
"""

import logging
import time

from ..dataio import load_tickets
from ..knowledge import KB
from ..llm import extract_json, get_agent_client
from ..state import Diagnosis, FacilityState

log = logging.getLogger("facilitymind.diagnose")


def _check_recurrence(ticket) -> bool:
    """同一位置同类故障若在当前工单之前已出现过，视为重复发生（升级处置）。"""
    history = load_tickets()
    try:
        cur = int(str(ticket["id"]).split("-")[-1])
    except (ValueError, KeyError):
        return False
    location = ticket.get("location", "")
    if not location or location == "未指定位置":
        return False
    earlier = [
        t
        for t in history
        if t.get("type") == ticket["type"]
        and int(str(t["id"]).split("-")[-1]) < cur
        and location in t.get("location_hint", "")
    ]
    return len(earlier) >= 1


def _single_diag(client, ticket: dict) -> dict | None:
    """让单个模型给出诊断 JSON；解析失败返回 None。"""
    sys_prompt = (
        "你是设施管理诊断专家。根据故障类型与工单描述，推断根因与处置建议。"
        "只返回 JSON：{root_cause, recommended_action, confidence}。"
    )
    out = client.complete(sys_prompt, ticket["raw"])
    parsed = extract_json(out)
    if parsed and parsed.get("root_cause") and parsed.get("recommended_action"):
        try:
            conf = float(parsed.get("confidence", 0.82))
        except (ValueError, TypeError):
            conf = 0.82
        return {
            "root_cause": str(parsed["root_cause"]),
            "recommended_action": str(parsed["recommended_action"]),
            "confidence": conf,
            "_model": client.name,
        }
    return None


def diagnose_agent(state: FacilityState) -> dict:
    ticket = state["ticket"]
    kb = KB.get(ticket["type"], KB["cleaning"])
    recurrence = _check_recurrence(ticket)
    client = get_agent_client("diagnose")

    start = time.time()
    log.info("[Diagnose] ▶ 开始诊断 ticket=%s 类型=%s 重复=%s", ticket["id"], ticket["type"], recurrence)

    # 出诊断：KB 默认结论 → 有 LLM 时让单模型推理
    root_cause = kb["root_cause"]
    recommended_action = kb["recommended_action"]
    confidence = 0.9 if recurrence else 0.82

    if client.available:
        parsed = _single_diag(client, ticket)
        if parsed:
            root_cause = parsed["root_cause"]
            recommended_action = parsed["recommended_action"]
            confidence = parsed["confidence"]

    diag: Diagnosis = {
        "root_cause": root_cause,
        "recommended_action": recommended_action,
        "required_skill": kb["required_skill"],
        "estimated_cost": kb["estimated_cost"],
        "sla_hours": kb["sla_hours"],
        "confidence": confidence,
        "recurrence": recurrence,
        "evidence": [],
    }

    log.info(
        "[Diagnose] ✔ 完成 用时=%.2fs 根因=%s；建议=%s；预估¥%.0f；SLA %sh；置信度=%.2f",
        time.time() - start, root_cause, recommended_action,
        kb["estimated_cost"], kb["sla_hours"], confidence,
    )
    return {
        "diagnosis": diag,
        "tool_calls": [],
        "messages": [{"role": "system", "content": f"[Diagnose] 类型={ticket['type']} → 根因={root_cause}；建议={recommended_action}；置信度={confidence:.2f}"}],
    }
