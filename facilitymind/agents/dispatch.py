"""Dispatch Agent：资源调度与派单建议。

职责：根据诊断所需技能，用 rank_vendors 服务按"性价比（成本省+质量高+响应快）"排序候选，
挑最划算的维保商；高价值决策（成本超限）的人工确认节点在 Approval 接入。
"""

import logging

from ..llm import get_agent_client
from ..services import rank_vendors
from ..state import Assignment, FacilityState

log = logging.getLogger("facilitymind.dispatch")


def dispatch_agent(state: FacilityState) -> dict:
    diag = state["diagnosis"]
    skill = diag["required_skill"]
    ttype = state["ticket"]["type"]

    ranked = rank_vendors(ttype, skill)
    best = ranked[0]
    rationale = (
        f"按性价比排序，{best['name']} 得分最高（成本¥{best['cost']:.0f} / "
        f"质量{best['quality']} / 响应{best['response_min']}分钟）"
    )

    client = get_agent_client("dispatch")
    if client.available:
        sys_prompt = (
            "你是物业调度助手。给定诊断所需技能与已按性价比排序的候选资源池，确认最优派单。"
            "只返回 JSON：{vendor, response_time_min, cost, rationale}。"
        )
        client.complete(sys_prompt, f"skill={skill}, ranked={ranked}")

    plan: Assignment = {
        "vendor": best["name"],
        "response_time_min": best["response_min"],
        "cost": best["cost"],
        "rationale": rationale,
    }
    log.info("[Dispatch] 派单 → %s（响应%s分钟 / ¥%.0f）：%s", best["name"], best["response_min"], best["cost"], rationale)
    return {"assignment": plan, "messages": [{"role": "system", "content": f"[Dispatch] 派单 → {best['name']}，预计{best['response_min']}分钟响应，报价¥{best['cost']:.0f}；{rationale}"}]}
