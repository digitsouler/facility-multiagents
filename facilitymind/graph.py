"""编排层：用 LangGraph 把多个 Agent 串成可控状态机。

工作流：Intake → Diagnose(ReAct) → Dispatch → Approval(HITL) → TechnicianReport(HITL) → QA(HITL) → Report。
- Approval：成本超阈值且非自动模式时 interrupt() 等审批人；
- TechnicianReport：派单后等师傅回传现场执行情况（非自动模式 interrupt）；
- QA：基于回传逐项核验，非自动模式 interrupt() 等复核人签字；
- 三处人工节点在 auto_approve=True 时全部自动放行，便于批量评估。
编译时挂 MemorySaver 检查点，使 interrupt 可暂停/恢复、状态可重放与审计。
"""

import logging
import time

from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, StateGraph

from .agents import (
    approval_agent,
    diagnose_agent,
    dispatch_agent,
    intake_agent,
    qa_agent,
    report_agent,
    technician_report_agent,
)
from .state import FacilityState

log = logging.getLogger("facilitymind.graph")


def _node(name: str):
    """节点包装：记录进入/完成与耗时；interrupt 正常透传（不记异常）。"""
    def deco(fn):
        def wrapper(state):
            start = time.time()
            log.info("[%s] ▶ 进入", name)
            try:
                res = fn(state)
            except GraphInterrupt:
                raise
            except Exception:
                log.exception("[%s] ✖ 执行异常", name)
                raise
            log.info("[%s] ✔ 完成 用时=%.2fs", name, time.time() - start)
            return res
        return wrapper
    return deco


def _route_after_approval(state: FacilityState) -> str:
    """审批被拒则终止流水线，否则继续回传与质检。"""
    approved = state.get("approval", {}).get("approved", True)
    return "proceed" if approved else "stop"


def build_graph():
    graph = StateGraph(FacilityState)
    graph.add_node("intake", _node("Intake")(intake_agent))
    graph.add_node("diagnose", _node("Diagnose")(diagnose_agent))
    graph.add_node("dispatch", _node("Dispatch")(dispatch_agent))
    graph.add_node("approval", _node("Approval")(approval_agent))
    graph.add_node("technician_report", _node("TechnicianReport")(technician_report_agent))
    graph.add_node("qa", _node("QA")(qa_agent))
    graph.add_node("report", _node("Report")(report_agent))

    graph.set_entry_point("intake")
    graph.add_edge("intake", "diagnose")
    graph.add_edge("diagnose", "dispatch")
    graph.add_edge("dispatch", "approval")
    graph.add_conditional_edges(
        "approval", _route_after_approval, {"proceed": "technician_report", "stop": END}
    )
    graph.add_edge("technician_report", "qa")
    graph.add_edge("qa", "report")
    graph.add_edge("report", END)

    return graph.compile(checkpointer=MemorySaver())


# 编译后的可执行图（模块级单例，供 API 复用）
app = build_graph()
