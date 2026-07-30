"""Diagnose Agent：用 ReAct 循环结合工具做根因诊断。

ReAct = 思考(Think) → 行动(Act，调工具) → 观察(Observe) → 再思考，直到足够确信。
工具做成进程内确定性函数（查知识库、读传感器桩），并保留 LLM 推理分支：
- 有 LLM：让模型在每一步决策调用哪个工具，或给出最终诊断；
- 无 LLM：走脚本化循环（先查 KB 再读传感器）演示同一套 ReAct 结构，保证离线可跑。
循环靠 max_iter / 置信度 / 无进展 三重控制，不会无限转圈。
"""

import logging
import time

from ..dataio import load_tickets
from ..knowledge import KB
from ..llm import extract_json, get_agent_client
from ..state import Diagnosis, FacilityState

log = logging.getLogger("facilitymind.diagnose")

MAX_ITER = 4


def _check_recurrence(ticket) -> bool:
    """同一位置同类故障若在当前工单之前已出现，视为重复发生。"""
    history = load_tickets()
    try:
        cur = int(str(ticket["id"]).split("-")[-1])
    except (ValueError, KeyError):
        return False
    location = ticket.get("location", "")
    if not location or location == "未指定位置":
        return False
    earlier = [
        t for t in history
        if t.get("type") == ticket["type"]
        and int(str(t["id"]).split("-")[-1]) < cur
        and location in t.get("location_hint", "")
    ]
    return len(earlier) >= 1


# --- 工具：进程内确定性函数，模拟外部系统调用 ---
def lookup_kb(fault_type: str) -> dict:
    """查故障知识库，返回该类型的根因/建议/技能/成本/SLA。"""
    return KB.get(fault_type, KB["cleaning"])


def read_sensor(fault_type: str) -> dict:
    """读传感器（桩）：离线返回确定性读数，真实接入时换成 MCP iot.read_sensor。"""
    return {"fault_type": fault_type, "status": "online", "note": "传感器桩（离线模拟，待接 MCP）"}


TOOLS = {"lookup_kb": lookup_kb, "read_sensor": read_sensor}


# 报修原文（如"电梯困人"）与知识库 key 不对应，需要归一化。
_NAME_MAP = {
    "电梯": "elevator", "电梯困人": "elevator", " HVAC": "hvac",
    "空调": "hvac", "空调不制冷": "hvac", "制冷": "hvac",
    "漏水": "leak", "渗水": "leak",
    "灯": "lighting", "照明": "lighting", "灯具": "lighting",
    "消防": "fire", "火警": "fire",
    "门禁": "access", "门": "access",
    "清洁": "cleaning", "保洁": "cleaning", "卫生": "cleaning",
    "绿化": "greening", "植物": "greening",
}


def _normalize_arg(arg: str, fallback: str) -> str:
    """把 LLM 可能给的报修原文/口语词映射成知识库标准 key。"""
    arg = str(arg or fallback).strip().lower()
    if arg in KB:
        return arg
    for cn, en in _NAME_MAP.items():
        if cn in arg:
            return en
    return fallback


def _decide_offline(client, ticket: dict, observations: dict) -> dict:
    """离线脚本化决策：先查 KB，再读传感器，之后收尾。"""
    if "lookup_kb" not in observations:
        return {"action": "lookup_kb", "arg": ticket["type"]}
    if "read_sensor" not in observations:
        return {"action": "read_sensor", "arg": ticket["type"]}
    return {"action": "finish"}


def _decide_llm(client, ticket: dict, observations: dict) -> dict:
    """让 LLM 决定下一步：调用工具 或 给出最终诊断。"""
    ttype = ticket["type"]
    sys_prompt = (
        f"你是设施管理诊断专家，用 ReAct 方式工作。可用工具：lookup_kb(故障类型)、read_sensor(故障类型)。"
        f"arg 必须使用标准化故障类型 '{ttype}'，不要传入报修原文。"
        "工作顺序：1) lookup_kb 查知识库；2) read_sensor 读传感器；3) finish 给出根因诊断。"
        '每步只返回 JSON：{"action":"lookup_kb"|"read_sensor"|"finish","arg":"' + ttype + '"} '
        'finish 时 {"action":"finish","root_cause":"...","recommended_action":"...","confidence":0.0~1.0}。'
    )
    obs_text = "; ".join(f"{k}={v}" for k, v in observations.items())
    user = f"工单：{ticket['raw']}\n已观察：{obs_text or '无'}"
    out = client.complete(sys_prompt, user)
    parsed = extract_json(out)
    log.info("[Diagnose][ReAct] LLM 决策=%s", parsed)
    if parsed and parsed.get("action") in TOOLS:
        return {"action": parsed["action"], "arg": _normalize_arg(parsed.get("arg"), ttype)}
    if parsed and parsed.get("action") == "finish":
        return parsed
    return {"action": "finish"}


def _build_diagnosis(kb: dict, ticket: dict, observations: dict, confidence: float) -> Diagnosis:
    recurrence = _check_recurrence(ticket)
    note = observations.get("read_sensor", {}).get("note", "")
    root_cause = kb["root_cause"]
    if note:
        root_cause = f"{root_cause}（{note}）"
    return {
        "root_cause": root_cause,
        "recommended_action": kb["recommended_action"],
        "required_skill": kb["required_skill"],
        "estimated_cost": kb["estimated_cost"],
        "sla_hours": kb["sla_hours"],
        "confidence": confidence,
        "recurrence": recurrence,
    }


def react_diagnose(ticket: dict, client) -> Diagnosis:
    """ReAct 诊断循环：思考→调工具→观察，直到收尾或达上限。"""
    observations: dict = {}
    called = set()
    decide = _decide_llm if (client and client.available) else _decide_offline
    ttype = ticket["type"]
    for i in range(MAX_ITER):
        # 两个工具都已观察，不再给 LLM 浪费步数，直接收尾。
        if "lookup_kb" in observations and "read_sensor" in observations:
            decision = {"action": "finish"}
        else:
            decision = decide(client, ticket, observations)
        action = decision.get("action")
        if action == "finish":
            kb = dict(lookup_kb(ttype))
            if decision.get("root_cause"):
                kb["root_cause"] = decision["root_cause"]
                kb["recommended_action"] = decision.get("recommended_action", kb["recommended_action"])
            conf = float(decision.get("confidence", 0.9 if _check_recurrence(ticket) else 0.82))
            return _build_diagnosis(kb, ticket, observations, conf)
        tool = TOOLS.get(action)
        if not tool:
            break
        arg = _normalize_arg(decision.get("arg"), ttype)
        # 同一工具已调过，改调另一个；两个都调过就收尾。
        if (action, arg) in called:
            other = next((a for a in TOOLS if (a, arg) not in called), None)
            if other:
                action, tool = other, TOOLS[other]
            else:
                break
        result = tool(arg)
        observations[action] = result
        called.add((action, arg))
        log.info("[Diagnose][ReAct] 第%d步 调 %s(%s) → %s", i + 1, action, arg, result)
    kb = observations.get("lookup_kb") or lookup_kb(ttype)
    return _build_diagnosis(kb, ticket, observations, 0.82)


def diagnose_agent(state: FacilityState) -> dict:
    ticket = state["ticket"]
    client = get_agent_client("diagnose")
    start = time.time()
    log.info("[Diagnose] ▶ 开始 ticket=%s 类型=%s", ticket["id"], ticket["type"])

    diag = react_diagnose(ticket, client)

    log.info(
        "[Diagnose] ✔ 完成 用时=%.2fs 根因=%s；建议=%s；预估¥%.0f；SLA %sh；置信度=%.2f",
        time.time() - start, diag["root_cause"], diag["recommended_action"],
        diag["estimated_cost"], diag["sla_hours"], diag["confidence"],
    )
    return {
        "diagnosis": diag,
        "messages": [{"role": "system",
                      "content": f"[Diagnose] 类型={ticket['type']} → 根因={diag['root_cause']}；建议={diag['recommended_action']}；置信度={diag['confidence']:.2f}"}],
    }
