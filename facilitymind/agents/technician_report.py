"""TechnicianReport Agent：师傅现场回传（Human-in-the-Loop）。

派单批准后，等待师傅把现场执行情况回传；这是质量闭环的关键人工入口。
- 非自动模式：interrupt() 暂停，等师傅录入回传（响应时长/影像/资质/备注）；
- 自动模式：用确定性模拟填充回传，保证批量评估不阻断。
"""

import hashlib
from langgraph.types import interrupt

from ..state import Assignment, Feedback, FacilityState


def _simulate_feedback(ticket: dict, plan: Assignment) -> Feedback:
    """确定性地推导一次"师傅回传"，保证离线可复现。"""
    seed = int(hashlib.md5(ticket["id"].encode()).hexdigest(), 16) % 1000
    actual_response = int(plan["response_time_min"] * (0.8 + (seed % 50) / 100.0))
    photos_uploaded = (seed % 4) != 0
    cert_verified = (seed % 10) != 0
    return {
        "technician": "系统模拟班组",
        "actual_response_min": actual_response,
        "photos_uploaded": photos_uploaded,
        "cert_verified": cert_verified,
        "completion_note": "现场已处置并恢复正常运行",
    }


def technician_report_agent(state: FacilityState) -> dict:
    ticket = state["ticket"]
    plan = state["assignment"]
    auto = state.get("auto_approve", False)

    if auto:
        feedback = _simulate_feedback(ticket, plan)
        note = "自动模式：系统模拟师傅回传"
    else:
        decision = interrupt({
            "prompt": f"工单 {ticket['id']} 处置完毕，请师傅回传现场执行情况。",
            "ticket_id": ticket["id"],
        })
        feedback = {
            "technician": decision.get("technician", "现场班组"),
            "actual_response_min": int(decision.get("actual_response_min", plan["response_time_min"])),
            "photos_uploaded": bool(decision.get("photos_uploaded", True)),
            "cert_verified": bool(decision.get("cert_verified", True)),
            "completion_note": decision.get("completion_note", "现场已处置"),
        }
        note = f"师傅 {feedback['technician']} 回传完毕"

    log_msg = f"[TechnicianReport] {note}；响应{feedback['actual_response_min']}分钟，影像={feedback['photos_uploaded']}，资质={feedback['cert_verified']}"
    return {"feedback": feedback, "messages": [{"role": "system", "content": log_msg}]}
