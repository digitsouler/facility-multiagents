"""编排层：用 LangGraph 把多个 Agent 串成可控状态机。

Phase 2 工作流：Intake → Diagnose → Dispatch → Approval(HITL) → QA → Report。
- Approval 节点使用 interrupt() 实现人工确认：高价值派单会暂停等待人决策；
- 条件边根据审批结果决定继续（proceed）或终止（stop）；
- QA 模拟现场执行并逐项核验；Report 生成结案与优化建议。
编译时挂 MemorySaver 检查点，使 interrupt 可暂停/恢复、状态可重放与审计。
"""

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from .agents import (
    approval_agent,
    diagnose_agent,
    dispatch_agent,
    intake_agent,
    qa_agent,
    report_agent,
)
from .state import FacilityState


def _route_after_approval(state: FacilityState) -> str:
    """审批被拒则终止流水线，否则继续质检与报告。"""
    approved = state.get("approval", {}).get("approved", True)
    return "proceed" if approved else "stop"


def build_graph():
    graph = StateGraph(FacilityState)
    graph.add_node("intake", intake_agent)
    graph.add_node("diagnose", diagnose_agent)
    graph.add_node("dispatch", dispatch_agent)
    graph.add_node("approval", approval_agent)
    graph.add_node("qa", qa_agent)
    graph.add_node("report", report_agent)

    graph.set_entry_point("intake")
    graph.add_edge("intake", "diagnose")
    graph.add_edge("diagnose", "dispatch")
    graph.add_edge("dispatch", "approval")
    graph.add_conditional_edges(
        "approval", _route_after_approval, {"proceed": "qa", "stop": END}
    )
    graph.add_edge("qa", "report")
    graph.add_edge("report", END)

    return graph.compile(checkpointer=MemorySaver())


# 编译后的可执行图（模块级单例，CLI/API 复用）
app = build_graph()
