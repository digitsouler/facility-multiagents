"""QA Agent：执行核验与质检评分。

职责：模拟现场执行结果（真实系统对接 CMMS 工单回写/iot 回传），并对照 QA 检查清单
逐项核验，输出通过/未通过与综合评分。

为在离线环境可复现，执行结果用工单 ID 做确定性哈希推导（同一工单每次结果一致），
这样既能演示"偶尔出现的影像缺失/资质未核验"等真实瑕疵，又不需要任何外部依赖。
"""

import hashlib

from ..knowledge import QA_CHECKLISTS
from ..state import ExecutionResult, FacilityState, QAResult


def _simulate(ticket: dict, plan: dict) -> ExecutionResult:
    """确定性地推导一次"现场执行结果"，保证离线可复现。"""
    seed = int(hashlib.md5(ticket["id"].encode()).hexdigest(), 16) % 1000
    # 实际响应时间在计划值的 0.8~1.3 倍之间浮动
    actual_response = int(plan["response_time_min"] * (0.8 + (seed % 50) / 100.0))
    photos_uploaded = (seed % 4) != 0     # 约 1/4 场景缺失影像留痕
    cert_verified = (seed % 10) != 0      # 约 1/10 场景未核验资质
    return {
        "actual_response_min": actual_response,
        "photos_uploaded": photos_uploaded,
        "cert_verified": cert_verified,
        "completion_note": "现场已处置并恢复正常运行",
    }


def qa_agent(state: FacilityState) -> dict:
    ticket = state["ticket"]
    diag = state["diagnosis"]
    plan = state["dispatch_plan"]
    exec_res = _simulate(ticket, plan)

    checks: list[dict] = []
    # 通用合规项：由模拟执行结果决定（跨所有故障类型都会核验）
    checks.append({"item": "维修前后影像留痕", "passed": exec_res["photos_uploaded"]})
    checks.append({"item": "作业人员资质核验", "passed": exec_res["cert_verified"]})
    # 类型专项清单（标准作业流程，模拟执行默认通过）
    for item in QA_CHECKLISTS.get(ticket["type"], []):
        checks.append({"item": item, "passed": True})

    # 硬规则检查：SLA 与成本预算
    sla_min = diag["sla_hours"] * 60
    sla_ok = exec_res["actual_response_min"] <= sla_min
    cost_ok = plan["cost"] <= diag["estimated_cost"] * 1.1
    checks.append({"item": f"响应时间≤SLA({sla_min}分钟)", "passed": sla_ok})
    checks.append({"item": f"成本≤预估(¥{diag['estimated_cost'] * 1.1:.0f})", "passed": cost_ok})

    issues = [c["item"] for c in checks if not c["passed"]]
    passed = len(issues) == 0
    score = round(sum(1 for c in checks if c["passed"]) / len(checks), 2)

    qa: QAResult = {
        "passed": passed,
        "score": score,
        "issues": issues,
        "checks": checks,
    }
    log = f"[QA] 通过={passed}，得分={score}，问题={issues or '无'}"
    return {"execution": exec_res, "qa": qa, "messages": [{"role": "system", "content": log}]}
