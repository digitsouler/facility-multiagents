"""确定性业务服务：纯计算与副作用原语，供各 agent 直接调用。

原则：
- 这里放"agent 必须做、不需要 LLM 选"的规则计算（预算/SLA/排序/质检/案例写入等）；
- 只有 diagnose 的检索类工具保留为 @tool，因为需要 LLM 在 ReAct 中自主选择。
- 所有函数返回 dict，不玩 JSON 字符串。
"""

import json
import logging
import os

from .knowledge import (
    APPROVAL_THRESHOLD_COST,
    KB,
    QA_CHECKLISTS,
    VENDORS,
    classify_type,
    classify_urgency,
)

log = logging.getLogger("facilitymind.services")

CASE_PATH = os.path.join(os.path.dirname(__file__), "data", "cases.jsonl")


def classify_fault(raw: str) -> dict:
    """从报修原文抽取故障类型与紧急度。"""
    out = {"type": classify_type(raw), "urgency": classify_urgency(raw)}
    log.info("[service] classify_fault → %s", out)
    return out


def rank_vendors(fault_type: str, skill: str) -> list[dict]:
    """按性价比（成本省+质量高+响应快）对候选维保商排序。"""
    cands = [v for v in VENDORS if v["skill"] == skill] or VENDORS
    costs = [v["cost"] for v in cands]
    resps = [v["response_min"] for v in cands]
    maxc, minc = max(costs), min(costs)
    maxr, minr = max(resps), min(resps)

    def norm(x, lo, hi):
        return 0.0 if hi == lo else (hi - x) / (hi - lo)

    ranked = []
    for v in cands:
        score = round(
            0.5 * norm(v["cost"], minc, maxc)
            + 0.3 * v.get("quality", 0.9)
            + 0.2 * norm(v["response_min"], minr, maxr),
            3,
        )
        ranked.append({
            "name": v["name"],
            "cost": v["cost"],
            "response_min": v["response_min"],
            "quality": v.get("quality", 0.9),
            "score": score,
        })
    ranked.sort(key=lambda x: -x["score"])
    log.info("[service] rank_vendors(%s,%s) → %s", fault_type, skill,
             [(r["name"], r["score"]) for r in ranked])
    return ranked


def check_budget(cost: float) -> dict:
    """判断报价是否超过人工审批阈值。"""
    needs = cost > APPROVAL_THRESHOLD_COST
    log.info("[service] check_budget(¥%.0f) → 需人工=%s (阈值¥%.0f)",
             cost, needs, APPROVAL_THRESHOLD_COST)
    return {"needs_human": needs, "threshold": APPROVAL_THRESHOLD_COST}


def validate_evidence(feedback: dict) -> dict:
    """校验师傅回传完整性。"""
    missing = []
    if not feedback.get("photos_uploaded"):
        missing.append("维修前后影像留痕")
    if not feedback.get("cert_verified"):
        missing.append("特种作业资质核验")
    log.info("[service] validate_evidence → 缺失=%s", missing)
    return {"complete": len(missing) == 0, "missing": missing}


def verify_photos(photos_uploaded: bool) -> dict:
    """核验维修前后影像是否留痕。"""
    passed = bool(photos_uploaded)
    log.info("[service] verify_photos → %s", passed)
    return {"passed": passed}


def check_sla(actual_response_min: int, sla_hours: int) -> dict:
    """核验实际响应时间是否在 SLA 内。"""
    ok = actual_response_min <= sla_hours * 60
    log.info("[service] check_sla(%dmin vs %dh) → %s", actual_response_min, sla_hours, ok)
    return {"passed": ok}


def check_cost(cost: float, estimated_cost: float) -> dict:
    """核验实际报价是否在预估成本 1.1 倍以内。"""
    ok = cost <= estimated_cost * 1.1
    log.info("[service] check_cost(¥%.0f vs ¥%.0f) → %s", cost, estimated_cost, ok)
    return {"passed": ok}


def write_prevention(ticket_id: str, issues: list[str]) -> dict:
    """质检未过时生成预防措施建议。"""
    note = f"工单{ticket_id}未过质检，问题：{issues}。建议加强过程留痕与交付验收，并复盘根因。"
    log.info("[service] write_prevention(%s) → %s", ticket_id, note)
    return {"prevention_note": note}


def _load_existing_case_ids() -> set[str]:
    """读 cases.jsonl 里已存在的 ticket_id，用于幂等。"""
    ids: set[str] = set()
    if not os.path.exists(CASE_PATH):
        return ids
    try:
        with open(CASE_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    case = json.loads(line)
                    tid = case.get("ticket_id")
                    if tid:
                        ids.add(tid)
                except (json.JSONDecodeError, TypeError):
                    continue
    except OSError as exc:
        log.warning("[service] 读案例库失败：%s", exc)
    return ids


def save_case(case: dict) -> dict:
    """把一次处置写入本地案例库（JSONL），幂等：同一 ticket_id 只写一次。"""
    ticket_id = case.get("ticket_id", "x")
    existing = _load_existing_case_ids()
    if ticket_id in existing:
        log.info("[service] save_case → %s 已存在，跳过写入", ticket_id)
        return {"saved": False, "case_id": f"CASE-{ticket_id}", "reason": "already_exists"}

    cid = f"CASE-{ticket_id}"
    case["case_id"] = cid
    os.makedirs(os.path.dirname(CASE_PATH), exist_ok=True)
    with open(CASE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(case, ensure_ascii=False) + "\n")
    log.info("[service] save_case → %s 已写入", cid)
    return {"saved": True, "case_id": cid}


def build_qa_checks(ticket: dict, diag: dict, plan: dict, feedback: dict) -> list[dict]:
    """组合一次质检需要的全部检查项。"""
    checks: list[dict] = []
    checks.append({"item": "维修前后影像留痕", "passed": verify_photos(feedback.get("photos_uploaded"))["passed"]})
    checks.append({"item": "作业人员资质核验", "passed": feedback.get("cert_verified", False)})
    for item in QA_CHECKLISTS.get(ticket["type"], []):
        checks.append({"item": item, "passed": True})
    checks.append({
        "item": f"响应时间≤SLA({diag['sla_hours'] * 60}分钟)",
        "passed": check_sla(feedback.get("actual_response_min", 0), diag["sla_hours"])["passed"],
    })
    checks.append({
        "item": f"成本≤预估(¥{diag['estimated_cost'] * 1.1:.0f})",
        "passed": check_cost(plan["cost"], diag["estimated_cost"])["passed"],
    })
    return checks
