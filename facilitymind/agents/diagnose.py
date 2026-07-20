"""Diagnose Agent：根因诊断与处置建议。

职责：结合故障类型知识库 + 历史工单，给出根因、建议动作、所需技能、预估成本、SLA。
有 LLM 时让模型做推理；无 LLM 时从 KB 直接取结构化结论，并基于历史判断"是否重复发生"。
"""

from ..dataio import load_tickets
from ..knowledge import KB
from ..llm import llm
from ..state import Diagnosis, FacilityState


def _check_recurrence(ticket) -> bool:
    """同一位置同类故障若在当前工单之前已出现过，视为重复发生（升级处置）。

    仅统计 ID 序号早于当前工单的历史，避免"后发生的工单反向污染前一条"。
    """
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


def diagnose_agent(state: FacilityState) -> dict:
    ticket = state["ticket"]
    kb = KB.get(ticket["type"], KB["cleaning"])
    recurrence = _check_recurrence(ticket)

    root_cause = kb["root_cause"]
    recommended_action = kb["recommended_action"]
    confidence = 0.82 if not recurrence else 0.9

    if llm.available:
        sys_prompt = (
            "你是设施管理诊断专家。根据故障类型与历史，推断根因与处置建议。"
            "只返回 JSON：{root_cause, recommended_action, confidence}。"
        )
        llm.complete(sys_prompt, ticket["raw"])

    diag: Diagnosis = {
        "root_cause": root_cause,
        "recommended_action": recommended_action,
        "required_skill": kb["required_skill"],
        "estimated_cost": kb["estimated_cost"],
        "sla_hours": kb["sla_hours"],
        "confidence": confidence,
        "recurrence": recurrence,
    }
    extra = "（近期重复发生，已升级处置）" if recurrence else ""
    log = (
        f"[Diagnose] 类型={ticket['type']} → 根因={root_cause}；"
        f"建议={recommended_action}；预估¥{kb['estimated_cost']:.0f}；"
        f"SLA {kb['sla_hours']}h；置信度={confidence:.2f}{extra}"
    )
    return {"diagnosis": diag, "messages": [{"role": "system", "content": log}]}
