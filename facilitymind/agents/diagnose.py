"""Diagnose Agent：根因诊断与处置建议。

职责：结合故障类型知识库 + IoT 实时遥测，给出根因、建议动作、所需技能、预估成本、SLA。
有 LLM 时让模型做推理；无 LLM 时回退到知识库确定性结论。
（经验记忆层后续基于 Redis / Qdrant 重建，ReAct 自主循环为后续可选增强；当前走确定性 + 单轮 LLM 路径。）
"""

import json
import logging
import time
from datetime import datetime

from ..dataio import load_tickets
from ..knowledge import KB
from ..llm import extract_json, get_agent_client, get_ensemble_clients
from ..mcp import providers
from ..state import Diagnosis, FacilityState, ToolCall

log = logging.getLogger("facilitymind.diagnose")


def _check_recurrence(ticket) -> bool:
    """同一位置同类故障若在当前工单之前已出现过，视为重复发生（升级处置）。"""
    history = load_tickets()
    try:
        cur = int(str(ticket["id"]).split("-")[-1])
    except (ValueError, KeyError):
        return False
    location = ticket.get("location", "")
    if not location or location == "未指定位置":
        return False
    earlier = [
        t
        for t in history
        if t.get("type") == ticket["type"]
        and int(str(t["id"]).split("-")[-1]) < cur
        and location in t.get("location_hint", "")
    ]
    return len(earlier) >= 1


def _single_diag(client, ticket: dict, sensor_ctx: str = "") -> dict | None:
    """让单个模型给出诊断 JSON；解析失败返回 None。sensor_ctx 为注入的现场遥测文本。"""
    sys_prompt = (
        "你是设施管理诊断专家。根据故障类型与现场遥测，推断根因与处置建议。"
        "只返回 JSON：{root_cause, recommended_action, confidence}。"
    )
    out = client.complete(sys_prompt, ticket["raw"] + sensor_ctx)
    parsed = extract_json(out)
    if parsed and parsed.get("root_cause") and parsed.get("recommended_action"):
        try:
            conf = float(parsed.get("confidence", 0.82))
        except (ValueError, TypeError):
            conf = 0.82
        return {
            "root_cause": str(parsed["root_cause"]),
            "recommended_action": str(parsed["recommended_action"]),
            "confidence": conf,
            "_model": client.name,
        }
    return None


def _synthesize(synth_client, candidates: list) -> dict | None:
    """用轻量合成器（默认/便宜模型）把多个候选诊断整合成最终一致结论。"""
    sys_prompt = (
        "你是诊断结论合成器。下面多个模型对同一工单给出了诊断，请综合它们、"
        "消除分歧，输出最终一致的根因与处置建议。只返回 JSON："
        "{root_cause, recommended_action, confidence}。"
    )
    user = "候选诊断：\n" + json.dumps(candidates, ensure_ascii=False, indent=2)
    out = synth_client.complete(sys_prompt, user)
    parsed = extract_json(out)
    if parsed and parsed.get("root_cause") and parsed.get("recommended_action"):
        try:
            conf = float(parsed.get("confidence", 0.82))
        except (ValueError, TypeError):
            conf = 0.82
        return {
            "root_cause": str(parsed["root_cause"]),
            "recommended_action": str(parsed["recommended_action"]),
            "confidence": conf,
        }
    return None


def _summarize_telemetry(telemetry: dict) -> str:
    """把遥测 dict 转成一句可用于佐证根因的文本（只列异常指标）。"""
    name = telemetry.get("name", telemetry.get("asset_id", "资产"))
    metrics = telemetry.get("metrics", {})
    bad = []
    for m, v in metrics.items():
        if isinstance(v, dict) and v.get("status") == "anomaly":
            bad.append(f"{m}={v.get('value')}{v.get('unit', '')}(基线{v.get('baseline')})")
    if not bad:
        return f"{name} 各指标正常"
    return f"{name}：" + "、".join(bad)


def _gather_sensor(ticket):
    """读取 IoT 实时遥测，返回 (telemetry|None, tool_call|None, obs_text)。"""
    asset_id = providers._resolve_asset(ticket)
    if not asset_id:
        log.info("[Diagnose] 该工单无对应受监控资产，跳过传感器")
        return None, None, "该工单无对应受监控资产，跳过传感器"
    try:
        res = providers.read_sensor_for_ticket(ticket)
    except Exception:  # noqa: BLE001
        log.warning("[Diagnose] 读取传感器异常，跳过", exc_info=True)
        return None, None, "读取传感器异常"
    if not res:
        log.info("[Diagnose] IoT 服务不可用 / 无数据，跳过传感器")
        return None, None, "IoT 服务不可用 / 无数据"
    ev = _summarize_telemetry(res)
    tc = {
        "agent": "diagnose",
        "server": "iot",
        "tool": "read_sensor",
        "args": {"asset_id": asset_id, "metric": "all"},
        "result": res,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    log.info("[Diagnose] IoT 遥测：%s", f"IoT({res.get('asset_id')})：{ev}")
    return res, tc, f"IoT({res.get('asset_id')})：{ev}"


def diagnose_agent(state: FacilityState) -> dict:
    ticket = state["ticket"]
    kb = KB.get(ticket["type"], KB["cleaning"])
    recurrence = _check_recurrence(ticket)
    ensemble = bool(state.get("ensemble"))
    client = get_agent_client("diagnose")

    start = time.time()
    log.info("[Diagnose] ▶ 开始诊断 ticket=%s 类型=%s 重复=%s", ticket["id"], ticket["type"], recurrence)

    # 拉取 IoT 实时遥测作为证据（无对应资产/服务不可用时自动跳过）
    telemetry = None
    tool_calls: list[ToolCall] = []
    tel, tc, obs = _gather_sensor(ticket)
    if tel:
        telemetry = tel
    if tc:
        tool_calls.append(tc)
    sensor_ctx = f"\n现场遥测：{obs}" if obs else ""

    # 出诊断：KB 默认结论 → 有 LLM 时让模型推理（可 Ensemble 多模型整合）
    root_cause = kb["root_cause"]
    recommended_action = kb["recommended_action"]
    confidence = 0.9 if recurrence else 0.82
    ensemble_used = False

    if ensemble:
        avail = [c for c in get_ensemble_clients() if c.available]
        candidates = [c for c in (_single_diag(c, ticket, sensor_ctx) for c in avail) if c]
        if candidates:
            ensemble_used = True
            if len(candidates) >= 2:
                synth = get_agent_client("diagnose")
                chosen = _synthesize(synth, candidates) if synth.available else None
                chosen = chosen or candidates[0]
            else:
                chosen = candidates[0]
            root_cause = chosen["root_cause"]
            recommended_action = chosen["recommended_action"]
            confidence = chosen["confidence"]
    else:
        if client.available:
            parsed = _single_diag(client, ticket, sensor_ctx)
            if parsed:
                root_cause = parsed["root_cause"]
                recommended_action = parsed["recommended_action"]
                confidence = parsed["confidence"]

    # 证据佐证：用真实传感器数据佐证根因
    evidence: list[str] = []
    if telemetry:
        ev = _summarize_telemetry(telemetry)
        evidence.append(f"IoT({telemetry.get('asset_id')})：{ev}")
        root_cause = f"{root_cause}（IoT佐证：{ev}）"

    diag: Diagnosis = {
        "root_cause": root_cause,
        "recommended_action": recommended_action,
        "required_skill": kb["required_skill"],
        "estimated_cost": kb["estimated_cost"],
        "sla_hours": kb["sla_hours"],
        "confidence": confidence,
        "recurrence": recurrence,
        "evidence": evidence,
    }

    ens_tag = " [Ensemble]" if ensemble_used else ""
    mcp_note = f" + IoT遥测({telemetry['asset_id']})" if telemetry else ""
    log.info(
        "[Diagnose] ✔ 完成 用时=%.2fs 根因=%s；建议=%s；预估¥%.0f；SLA %sh；置信度=%.2f%s%s",
        time.time() - start, root_cause, recommended_action,
        kb["estimated_cost"], kb["sla_hours"], confidence, ens_tag, mcp_note,
    )
    return {
        "diagnosis": diag,
        "tool_calls": tool_calls,
        "messages": [{"role": "system", "content": f"[Diagnose{ens_tag}{mcp_note}] 类型={ticket['type']} → 根因={root_cause}；建议={recommended_action}；置信度={confidence:.2f}"}],
    }
