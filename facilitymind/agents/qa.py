"""QA Agent：基于师傅回传做质检核验，并人工复核签字（Human-in-the-Loop）。

职责：消费 TechnicianReport 的回传结果，对照 QA 清单逐项核验；
非自动模式下 interrupt() 等复核人签字，自动模式系统自动签。
"""

from datetime import datetime

from langgraph.types import interrupt

from ..knowledge import QA_CHECKLISTS
from ..state import FacilityState, QAResult


def qa_agent(state: FacilityState) -> dict:
    ticket = state["ticket"]
    diag = state["diagnosis"]
    plan = state["assignment"]
    fb = state.get("feedback", {})
    auto = state.get("auto_approve", False)

    checks: list[dict] = []
    checks.append({"item": "维修前后影像留痕", "passed": fb.get("photos_uploaded", False)})
    checks.append({"item": "作业人员资质核验", "passed": fb.get("cert_verified", False)})
    for item in QA_CHECKLISTS.get(ticket["type"], []):
        checks.append({"item": item, "passed": True})

    sla_min = diag["sla_hours"] * 60
    sla_ok = fb.get("actual_response_min", 0) <= sla_min
    cost_ok = plan["cost"] <= diag["estimated_cost"] * 1.1
    checks.append({"item": f"响应时间≤SLA({sla_min}分钟)", "passed": sla_ok})
    checks.append({"item": f"成本≤预估(¥{diag['estimated_cost'] * 1.1:.0f})", "passed": cost_ok})

    issues = [c["item"] for c in checks if not c["passed"]]
    passed = len(issues) == 0
    score = round(sum(1 for c in checks if c["passed"]) / len(checks), 2)

    if auto:
        reviewer = "system"
        review_note = "自动模式：系统自动复核"
    else:
        decision = interrupt({
            "prompt": f"工单 {ticket['id']} 质检{'通过' if passed else '未完全通过'}，请复核签字。",
            "ticket_id": ticket["id"],
            "issues": issues,
        })
        reviewer = decision.get("reviewer", "质检主管")
        review_note = decision.get("review_note", "已复核签字")

    qa: QAResult = {
        "passed": passed,
        "score": score,
        "issues": issues,
        "checks": checks,
        "reviewer": reviewer,
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "review_note": review_note,
    }
    log = f"[QA] 通过={passed}，得分={score}，复核人={reviewer}；问题={issues or '无'}"
    return {"qa": qa, "messages": [{"role": "system", "content": log}]}
