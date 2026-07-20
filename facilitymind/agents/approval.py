"""Approval Agent：Human-in-the-Loop 人工确认节点。

这是企业落地多 Agent 系统的关键管控点——高价值或高风险决策不自动放行，
而是暂停并等待人确认。实现上用 LangGraph 的 interrupt()：
- 成本超过阈值且非自动模式时，调用 interrupt() 暂停工作流，把待确认信息交给调用方；
- 调用方（CLI/前端）拿到人的决策后，用 Command(resume=decision) 恢复执行；
- 恢复后根据决策写入 approval 状态。
成本未超阈值（或批量/自动模式）则系统自动通过，不阻断流水线。
"""

from datetime import datetime

from langgraph.types import interrupt

from ..knowledge import APPROVAL_THRESHOLD_COST
from ..state import Approval, FacilityState


def approval_agent(state: FacilityState) -> dict:
    plan = state["dispatch_plan"]
    ticket = state["ticket"]
    cost = plan.get("cost", 0.0)
    needs_human = (cost > APPROVAL_THRESHOLD_COST) and not state.get("auto_approve", False)

    if needs_human:
        decision = interrupt(
            {
                "prompt": (
                    f"工单 {ticket['id']} 派单报价 ¥{cost:.0f} 超过自动审批阈值 "
                    f"¥{APPROVAL_THRESHOLD_COST:.0f}，需人工确认是否批准。"
                ),
                "plan": plan,
                "ticket_id": ticket["id"],
            }
        )
        approved = bool(decision.get("approved", False))
        note = decision.get("note", "")
        approver = decision.get("approver", "现场主管")
        status = "approved" if approved else "rejected"
    else:
        approved = True
        status = "auto_approved"
        approver = "system"
        if cost > APPROVAL_THRESHOLD_COST:
            note = "批量/自动模式跳过人工确认"
        else:
            note = "成本未超阈值，系统自动通过"

    approval: Approval = {
        "status": status,
        "approved": approved,
        "note": note,
        "decided_at": datetime.now().isoformat(timespec="seconds"),
        "approver": approver,
    }
    log = f"[Approval] 状态={status}，批准={approved}，审批人={approver}；{note}"
    return {"approval": approval, "messages": [{"role": "system", "content": log}]}
