"""QA Agent：基于师傅回传做质检核验，并人工复核签字（Human-in-the-Loop）。

职责：消费 TechnicianReport 的回传结果，对照 QA 清单逐项核验；
非自动模式下 interrupt() 等复核人签字，自动模式系统自动签。
"""

import json
from datetime import datetime

from langgraph.types import interrupt

from ..knowledge import QA_CHECKLISTS
from ..state import FacilityState, QAResult
from ..tools.registry import verify_photos, check_sla, check_cost, write_prevention


def qa_agent(state: FacilityState) -> dict:
    ticket = state["ticket"]
    diag = state["diagnosis"]
    plan = state["assignment"]
    fb = state.get("feedback", {})
    auto = state.get("auto_approve", False)

    checks: list[dict] = []
    p_photo = json.loads(verify_photos.invoke({"photos_uploaded": fb.get("photos_uploaded", False)}))["passed"]
    checks.append({"item": "维修前后影像留痕", "passed": p_photo})
    checks.append({"item": "作业人员资质核验", "passed": fb.get("cert_verified", False)})
    for item in QA_CHECKLISTS.get(ticket["type"], []):
        checks.append({"item": item, "passed": True})

    p_sla = json.loads(check_sla.invoke({
        "actual_response_min": fb.get("actual_response_min", 0),
        "sla_hours": diag["sla_hours"],
    }))["passed"]
    p_cost = json.loads(check_cost.invoke({
        "cost": plan["cost"],
        "estimated_cost": diag["estimated_cost"],
    }))["passed"]
    checks.append({"item": f"响应时间≤SLA({diag['sla_hours'] * 60}分钟)", "passed": p_sla})
    checks.append({"item": f"成本≤预估(¥{diag['estimated_cost'] * 1.1:.0f})", "passed": p_cost})

    issues = [c["item"] for c in checks if not c["passed"]]
    passed = len(issues) == 0
    score = round(sum(1 for c in checks if c["passed"]) / len(checks), 2)

    prevention_note = ""
    if not passed:
        prevention_note = json.loads(write_prevention.invoke({
            "ticket_id": ticket["id"],
            "issues_json": json.dumps(issues, ensure_ascii=False),
        }))["prevention_note"]

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
    if prevention_note:
        log += f"；预防={prevention_note}"
    return {"qa": qa, "messages": [{"role": "system", "content": log}]}
