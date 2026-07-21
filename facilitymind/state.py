"""共享状态定义：多 Agent 工作流在节点间传递的数据结构。

使用 TypedDict 而非 pydantic，是因为 LangGraph 原生基于 TypedDict 做状态归并（reducer）。
这样每个 Agent 节点只需返回自己负责的那部分字段。
"""

from typing import Annotated, Optional, TypedDict
import operator
from langgraph.graph import add_messages


class Ticket(TypedDict):
    """结构化工单：由 Intake Agent 产出。"""
    id: str
    raw: str                 # 原始报修文本
    type: str                # 故障类型：elevator / hvac / leak / lighting / fire / access / cleaning / greening
    urgency: str             # high / medium / low
    location: str            # 位置描述，如 "A座3#梯"
    location_hint: str       # 原始结构化位置提示（用于映射到受监控资产/MCP）
    reporter: str            # 报修人
    created_at: str          # 创建时间


class Diagnosis(TypedDict):
    """诊断结论：由 Diagnose Agent 产出。"""
    root_cause: str          # 推断的根因
    recommended_action: str  # 建议处置动作
    required_skill: str      # 需要的技能标签
    estimated_cost: float    # 预估成本（元）
    sla_hours: int           # 服务级别时限（小时）
    confidence: float        # 置信度 0-1
    recurrence: bool         # 是否近期重复发生
    evidence: list[str]      # 来自 MCP 工具的真实数据佐证（无则空）


class DispatchPlan(TypedDict):
    """派单方案：由 Dispatch Agent 产出。"""
    vendor: str
    response_time_min: int   # 预计响应时间（分钟）
    cost: float              # 实际报价（元）
    rationale: str           # 选择理由


class Approval(TypedDict):
    """人工确认结果：由 Approval（HITL）节点产出。"""
    status: str              # auto_approved / approved / rejected
    approved: bool
    note: str                # 审批备注
    decided_at: str          # 决策时间
    approver: str            # 审批人


class ExecutionResult(TypedDict):
    """模拟现场执行结果：供 QA Agent 核验（真实系统对接 CMMS 工单回写）。"""
    actual_response_min: int
    photos_uploaded: bool    # 维修前后影像留痕
    cert_verified: bool      # 特种作业资质核验
    completion_note: str


class QAResult(TypedDict):
    """质检结论：由 QA Agent 产出。"""
    passed: bool
    score: float             # 0-1 综合评分
    issues: list[str]        # 未通过项
    checks: list[dict]       # 逐项明细 {item, passed}


class Report(TypedDict):
    """结案报告：由 Report Agent 产出。"""
    summary: str
    recommendations: list[str]
    metrics: dict


class ToolCall(TypedDict, total=False):
    """一次 MCP 工具调用轨迹（供 Dashboard「工具调用时间线」展示）。"""
    agent: str               # 发起调用的 Agent
    server: str              # MCP server 名
    tool: str                # 工具名
    args: dict               # 入参
    result: object           # 返回（截断展示用）
    error: str               # 若有错误
    ts: str                  # 时间戳


class FacilityState(TypedDict, total=False):
    """整条工作流的状态。total=False 允许节点只返回自己关心的字段。"""
    ticket: Ticket
    diagnosis: Diagnosis
    dispatch_plan: DispatchPlan
    approval: Approval
    execution: ExecutionResult
    qa: QAResult
    report: Report
    auto_approve: bool       # 批量/非交互模式下跳过人工确认
    ensemble: bool           # 诊断阶段是否启用多模型集成（Ensemble）
    tool_calls: Annotated[list, operator.add]  # 全链路 MCP 工具调用轨迹
    messages: Annotated[list, add_messages]  # 全程可追溯的 Agent 对话日志
