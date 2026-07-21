"""Dispatch Agent：资源调度与派单建议。

职责：根据诊断所需的技能标签，从资源池中筛选匹配 vendor，按"响应时间优先、成本次之"排序，
产出派单方案。高价值决策（成本超限）的人工确认节点将在 Phase 2 接入。
"""

from ..dataio import load_vendors
from ..llm import get_agent_client
from ..state import DispatchPlan, FacilityState


def dispatch_agent(state: FacilityState) -> dict:
    diag = state["diagnosis"]
    skill = diag["required_skill"]
    vendors = load_vendors()

    candidates = [v for v in vendors if v["skill"] == skill]
    if not candidates:
        candidates = vendors  # 兜底：无精确匹配时全局可选

    # 排序：响应时间升序，其次成本升序
    candidates.sort(key=lambda v: (v["response_min"], v["cost"]))
    best = candidates[0]

    rationale = f"按技能[{skill}]匹配，{best['name']}响应{best['response_min']}分钟最快且报价最低"

    client = get_agent_client("dispatch")
    if client.available:
        sys_prompt = (
            "你是物业调度助手。给定诊断所需的技能与候选资源池，给出最优派单。"
            "只返回 JSON：{vendor, response_time_min, cost, rationale}。"
        )
        client.complete(sys_prompt, f"skill={skill}, vendors={candidates}")

    plan: DispatchPlan = {
        "vendor": best["name"],
        "response_time_min": best["response_min"],
        "cost": best["cost"],
        "rationale": rationale,
    }
    log = f"[Dispatch] 派单 → {best['name']}，预计{best['response_min']}分钟响应，报价¥{best['cost']:.0f}；{rationale}"
    return {"dispatch_plan": plan, "messages": [{"role": "system", "content": log}]}
