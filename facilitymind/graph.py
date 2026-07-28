"""编排层：用 LangGraph 把多个 Agent 串成可控状态机。

Phase 2 工作流：Intake → Diagnose → Dispatch → Approval(HITL) → QA → Report。
- Approval 节点使用 interrupt() 实现人工确认：高价值派单会暂停等待人决策；
- 条件边根据审批结果决定继续（proceed）或终止（stop）；
- QA 模拟现场执行并逐项核验；Report 生成结案与优化建议。
编译时挂 MemorySaver 检查点，使 interrupt 可暂停/恢复、状态可重放与审计。
"""

import logging
import time

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

log = logging.getLogger("facilitymind.graph")


def _node(name: str):
    """节点包装：记录进入/完成与耗时，异常透传并记日志。"""
    def deco(fn):
        def wrapper(state):
            start = time.time()
            log.info("[%s] ▶ 进入", name)
            try:
                res = fn(state)
            except Exception:
                log.exception("[%s] ✖ 执行异常", name)
                raise
            log.info("[%s] ✔ 完成 用时=%.2fs", name, time.time() - start)
            return res
        return wrapper
    return deco


def _route_after_approval(state: FacilityState) -> str:
    """审批被拒则终止流水线，否则继续质检与报告。"""
    approved = state.get("approval", {}).get("approved", True)
    return "proceed" if approved else "stop"


def build_graph():
    graph = StateGraph(FacilityState)
    graph.add_node("intake", _node("Intake")(intake_agent))
    graph.add_node("diagnose", _node("Diagnose")(diagnose_agent))
    graph.add_node("dispatch", _node("Dispatch")(dispatch_agent))
    graph.add_node("approval", _node("Approval")(approval_agent))
    graph.add_node("qa", _node("QA")(qa_agent))
    graph.add_node("report", _node("Report")(report_agent))

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
