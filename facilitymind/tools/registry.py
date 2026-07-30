"""工具注册中心：所有 LangGraph @tool 在本文件定义，按名称取用，避免散落到各 agent。

分层：
- 进程内确定性工具（查知识库 / 案例检索桩 / 派单排序 / 预算核验 / 质检核验 / 案例写入等）用 @tool 定义，
  由使用方直接调用或经子图内 ToolNode 执行；
- 跨系统工具（IoT/CMMS/ERP/IM）走 MCP：read_sensor 经 mcp_client 调本地 MCP server，
  失败自动回退桩；未来接真系统只改 mcp 层，agent 调用方式不变。

工具统一返回 JSON 字符串：ToolNode 生成的 ToolMessage.content 为字符串最稳妥，调用方按需 json.loads。
"""

import json
import logging
import os

from langchain_core.tools import tool

from ..knowledge import KB, VENDORS, APPROVAL_THRESHOLD_COST

log = logging.getLogger("facilitymind.tools")

CASE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "cases.jsonl")


# --------------------------------------------------------------------------- #
# 诊断工具
# --------------------------------------------------------------------------- #
@tool
def lookup_kb(fault_type: str) -> str:
    """查故障知识库，返回该类型的根因/建议/所需技能/预估成本/SLA。fault_type 用标准化英文 key（如 elevator、hvac）。"""
    data = KB.get(fault_type, KB["cleaning"])
    log.info("[tool] lookup_kb(%s) → %s", fault_type, list(data))
    return json.dumps(data, ensure_ascii=False)


@tool
def read_sensor(fault_type: str) -> str:
    """经 MCP 读取 IoT 传感器实时读数（真实协议，本地 server 模拟数据），返回该故障类型下各资产读数。
    fault_type 用标准化 key。MCP 不可用时自动回退模拟桩。"""
    from .mcp_client import read_iot_sensor

    data = read_iot_sensor(fault_type)
    rows = data.get("rows", [])
    log.info("[tool] read_sensor(%s) 来源=%s 行数=%d → %s", fault_type, data.get("source"), len(rows), rows)
    return json.dumps(data, ensure_ascii=False)


@tool
def recall_cases(fault_type: str, location: str = "") -> str:
    """检索相似历史工单（好案例优先）。当前为本地桩返回空列表；后续接 Qdrant 改成语义检索。"""
    log.info("[tool] recall_cases(%s, %s) → []", fault_type, location)
    return json.dumps([], ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 受理工具
# --------------------------------------------------------------------------- #
@tool
def classify_fault(raw: str) -> str:
    """从报修原文抽取故障类型与紧急度，返回 {type, urgency}。"""
    from ..knowledge import classify_type, classify_urgency

    out = {"type": classify_type(raw), "urgency": classify_urgency(raw)}
    log.info("[tool] classify_fault → %s", out)
    return json.dumps(out, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 派单工具
# --------------------------------------------------------------------------- #
@tool
def rank_vendors(fault_type: str, skill: str) -> str:
    """按性价比（成本省+质量高+响应快）对候选维保商排序，返回排序后的名单与得分。
    fault_type 标准化 key；skill 为诊断所需技能标签。"""
    cands = [v for v in VENDORS if v["skill"] == skill] or VENDORS
    costs = [v["cost"] for v in cands]
    resps = [v["response_min"] for v in cands]
    maxc, minc = max(costs), min(costs)
    maxr, minr = max(resps), min(resps)
    norm = lambda x, lo, hi: 0.0 if hi == lo else (hi - x) / (hi - lo)  # noqa: E731

    ranked = []
    for v in cands:
        score = round(
            0.5 * norm(v["cost"], minc, maxc)
            + 0.3 * v.get("quality", 0.9)
            + 0.2 * norm(v["response_min"], minr, maxr),
            3,
        )
        ranked.append({
            "name": v["name"], "cost": v["cost"],
            "response_min": v["response_min"], "quality": v.get("quality", 0.9),
            "score": score,
        })
    ranked.sort(key=lambda x: -x["score"])
    log.info("[tool] rank_vendors(%s,%s) → %s", fault_type, skill, [(r["name"], r["score"]) for r in ranked])
    return json.dumps(ranked, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 审批工具
# --------------------------------------------------------------------------- #
@tool
def check_budget(cost: float) -> str:
    """判断派单报价是否超过人工审批阈值，返回是否需要人工确认。"""
    needs = cost > APPROVAL_THRESHOLD_COST
    log.info("[tool] check_budget(¥%.0f) → 需人工=%s (阈值¥%.0f)", cost, needs, APPROVAL_THRESHOLD_COST)
    return json.dumps({"needs_human": needs, "threshold": APPROVAL_THRESHOLD_COST})


# --------------------------------------------------------------------------- #
# 师傅回传工具
# --------------------------------------------------------------------------- #
@tool
def validate_evidence(feedback_json: str) -> str:
    """校验师傅回传完整性（影像/资质是否齐备），返回缺失项。feedback_json 为 JSON 字符串。"""
    try:
        fb = json.loads(feedback_json)
    except (json.JSONDecodeError, TypeError):
        fb = {}
    missing = []
    if not fb.get("photos_uploaded"):
        missing.append("维修前后影像留痕")
    if not fb.get("cert_verified"):
        missing.append("特种作业资质核验")
    log.info("[tool] validate_evidence → 缺失=%s", missing)
    return json.dumps({"complete": len(missing) == 0, "missing": missing})


# --------------------------------------------------------------------------- #
# 质检工具
# --------------------------------------------------------------------------- #
@tool
def verify_photos(photos_uploaded: bool) -> str:
    """核验维修前后影像是否留痕。"""
    log.info("[tool] verify_photos → %s", photos_uploaded)
    return json.dumps({"passed": bool(photos_uploaded)})


@tool
def check_sla(actual_response_min: int, sla_hours: int) -> str:
    """核验实际响应时间是否在 SLA 内。"""
    ok = actual_response_min <= sla_hours * 60
    log.info("[tool] check_sla(%dmin vs %dh) → %s", actual_response_min, sla_hours, ok)
    return json.dumps({"passed": ok})


@tool
def check_cost(cost: float, estimated_cost: float) -> str:
    """核验实际报价是否在预估成本 1.1 倍以内。"""
    ok = cost <= estimated_cost * 1.1
    log.info("[tool] check_cost(¥%.0f vs ¥%.0f) → %s", cost, estimated_cost, ok)
    return json.dumps({"passed": ok})


@tool
def write_prevention(ticket_id: str, issues_json: str) -> str:
    """当质检未过时生成预防措施建议（复盘/写回知识库用）。"""
    try:
        issues = json.loads(issues_json)
    except (json.JSONDecodeError, TypeError):
        issues = []
    note = f"工单{ticket_id}未过质检，问题：{issues}。建议加强过程留痕与交付验收，并复盘根因。"
    log.info("[tool] write_prevention(%s) → %s", ticket_id, note)
    return json.dumps({"prevention_note": note})


# --------------------------------------------------------------------------- #
# 结案工具
# --------------------------------------------------------------------------- #
@tool
def save_case(case_json: str) -> str:
    """把一次处置（工单/诊断/派单/质检）写入本地案例库（JSONL），供后续好案例复用。"""
    try:
        case = json.loads(case_json)
    except (json.JSONDecodeError, TypeError):
        case = {}
    cid = f"CASE-{case.get('ticket_id', 'x')}"
    case["case_id"] = cid
    os.makedirs(os.path.dirname(CASE_PATH), exist_ok=True)
    with open(CASE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(case, ensure_ascii=False) + "\n")
    log.info("[tool] save_case → %s 已写入", cid)
    return json.dumps({"saved": True, "case_id": cid})


_REGISTRY: dict = {
    "lookup_kb": lookup_kb,
    "read_sensor": read_sensor,
    "recall_cases": recall_cases,
    "classify_fault": classify_fault,
    "rank_vendors": rank_vendors,
    "check_budget": check_budget,
    "validate_evidence": validate_evidence,
    "verify_photos": verify_photos,
    "check_sla": check_sla,
    "check_cost": check_cost,
    "write_prevention": write_prevention,
    "save_case": save_case,
}


def get_tool(name: str):
    return _REGISTRY[name]


def all_tools() -> list:
    return list(_REGISTRY.values())
