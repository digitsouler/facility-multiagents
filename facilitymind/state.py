"""多 Agent 工作流在节点间传递的共享状态，基于 TypedDict 以便 LangGraph 做状态归并。"""

from typing import Annotated, TypedDict
import operator
from langgraph.graph import add_messages


class Ticket(TypedDict):
    """结构化工单，由 Intake 产出。"""
    id: str
    raw: str                 # 原始报修文本
    type: str                # 故障类型：elevator/hvac/leak/lighting/fire/access/cleaning/greening
    urgency: str             # high/medium/low
    location: str            # 位置描述，如 "A座3#梯"
    location_hint: str       # 原始结构化位置提示
    reporter: str            # 报修人
    created_at: str          # 创建时间


class Diagnosis(TypedDict):
    """诊断结论，由 Diagnose 产出。"""
    root_cause: str
    recommended_action: str
    required_skill: str
    estimated_cost: float
    sla_hours: int
    confidence: float
    recurrence: bool


class Assignment(TypedDict):
    """派单方案，由 Dispatch 产出。"""
    vendor: str
    response_time_min: int
    cost: float
    rationale: str


class Approval(TypedDict):
    """人工审批结果，由 Approval(HITL) 产出。"""
    status: str              # auto_approved/approved/rejected
    approved: bool
    note: str
    decided_at: str
    approver: str


class Feedback(TypedDict):
    """师傅现场回传，由 TechnicianReport(HITL) 产出。"""
    technician: str          # 执行师傅/班组
    actual_response_min: int
    photos_uploaded: bool    # 维修前后影像留痕
    cert_verified: bool      # 特种作业资质核验
    completion_note: str


class QAResult(TypedDict):
    """质检结论，由 QA 产出。"""
    passed: bool
    score: float
    issues: list[str]
    checks: list[dict]       # 逐项明细 {item, passed}
    reviewer: str            # 复核人
    reviewed_at: str
    review_note: str


class Report(TypedDict):
    """结案报告，由 Report 产出。"""
    summary: str
    recommendations: list[str]
    metrics: dict


class FacilityState(TypedDict, total=False):
    """整条工作流的状态。total=False 允许节点只返回自己关心的字段。"""
    ticket: Ticket
    diagnosis: Diagnosis
    assignment: Assignment
    approval: Approval
    feedback: Feedback
    qa: QAResult
    report: Report
    auto_approve: bool       # 批量/非交互模式跳过全部人工确认
    messages: Annotated[list, add_messages]
