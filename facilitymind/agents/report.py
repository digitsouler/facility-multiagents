"""Report Agent：结案报告与优化建议。

职责：汇总整条流水线的关键结论，并基于诊断/质检/审批信息给出可执行的优化建议。
这是"复盘闭环"的最后一环——把一次处置沉淀为可复用的运营洞察。
"""

import logging
import time

from ..memory.qdrant_cases import save as qdrant_save
from ..memory.redis_vendor import update as redis_update
from ..state import FacilityState, Report

log = logging.getLogger("facilitymind.report")


def report_agent(state: FacilityState) -> dict:
    ticket = state["ticket"]
    diag = state["diagnosis"]
    plan = state["assignment"]
    approval = state.get("approval", {})
    qa = state.get("qa", {})
    feedback = state.get("feedback", {})

    recs: list[str] = []
    if diag.get("recurrence"):
        recs.append(
            "该位置/设备近期重复发生同类故障，建议纳入预防性维护（PM）计划，"
            "并安排专项巡检与根因治理。"
        )
    if plan["cost"] > 2000:
        recs.append(
            "属高价值工单，建议评估年度维保框架合同以锁定单价，降低单次处置成本。"
        )
    if not qa.get("passed", True):
        for iss in qa.get("issues", []):
            recs.append(f"针对「{iss}」加强 vendor 过程管理与交付验收留痕。")
    if not recs:
        recs.append("本次处置规范、闭环良好，建议沉淀为标准作业知识库条目供后续复用。")

    qa_passed = qa.get("passed", True)
    summary = (
        f"工单 {ticket['id']}（{ticket['type']} / 紧急度 {ticket['urgency']}）于 "
        f"{approval.get('decided_at', '-')} 经 {approval.get('approver', '-')} 处置，"
        f"根因为「{diag['root_cause']}」，由 {plan['vendor']} 完成，"
        f"报价 ¥{plan['cost']:.0f}，质检{'通过' if qa_passed else '未完全通过'}。"
    )

    report: Report = {
        "summary": summary,
        "recommendations": recs,
        "metrics": {
            "cost": plan["cost"],
            "sla_hours": diag["sla_hours"],
            "qa_score": qa.get("score", 0.0),
            "actual_response_min": feedback.get("actual_response_min", 0),
            "recurrence": diag.get("recurrence", False),
        },
    }

    # 把本次处置写入案例库（Qdrant 向量 + 本地 JSONL 双写；好/坏都存，坏案例供预防检索）
    case = {
        "ticket_id": ticket["id"],
        "case_id": f"CASE-{ticket['id']}",
        "type": ticket["type"],
        "location": ticket.get("location", ""),
        "root_cause": diag["root_cause"],
        "recommended_action": diag["recommended_action"],
        "vendor": plan["vendor"],
        "cost": plan["cost"],
        "qa_passed": qa_passed,
        "qa_score": qa.get("score", 0.0),
        "outcome": "good" if qa_passed else "bad",
        "recurrence": diag.get("recurrence", False),
        "recommendations": recs,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    saved = qdrant_save(case)
    # 回写维保商口碑（Redis 滚动质量/成本）
    redis_update(ticket["id"], plan["vendor"], plan["cost"], qa_passed)
    log.info("[Report] %s 案例落库=%s", summary, saved.get("case_id"))

    return {"report": report, "messages": [{"role": "system", "content": summary}]}
