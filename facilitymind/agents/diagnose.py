"""Diagnose Agent：根因诊断与处置建议。

职责：结合故障类型知识库 + 历史工单，给出根因、建议动作、所需技能、预估成本、SLA。
有 LLM 时让模型做推理；无 LLM 时从 KB 直接取结构化结论，并基于历史判断"是否重复发生"。
开启 ensemble 时，扇出多个模型并用合成器整合最终根因/处置。
"""

import json
from datetime import datetime

from ..dataio import load_tickets
from ..knowledge import KB
from ..llm import extract_json, get_agent_client, get_ensemble_clients
from ..mcp import providers
from ..memory.retrieval import retrieve_similar, get_asset_context, get_kb_context, format_memory_context
from ..state import Diagnosis, FacilityState, ToolCall


def _check_recurrence(ticket) -> bool:
    """同一位置同类故障若在当前工单之前已出现过，视为重复发生（升级处置）。

    仅统计 ID 序号早于当前工单的历史，避免"后发生的工单反向污染前一条"。
    """
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


def _single_diag(client, ticket: dict, memory_ctx: str = "") -> dict | None:
    """让单个模型给出诊断 JSON；解析失败返回 None。memory_ctx 为注入的历史经验文本。"""
    sys_prompt = (
        "你是设施管理诊断专家。根据故障类型与历史，推断根因与处置建议。"
        "只返回 JSON：{root_cause, recommended_action, confidence}。"
    )
    out = client.complete(sys_prompt, ticket["raw"] + memory_ctx)
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


def diagnose_agent(state: FacilityState) -> dict:
    ticket = state["ticket"]
    kb = KB.get(ticket["type"], KB["cleaning"])
    recurrence = _check_recurrence(ticket)

    # 检索持久化记忆：相似历史事件 + 资产档案 + 沉淀知识，注入 LLM 提示词。
    # 记忆不可用时各检索均返回空，照常走 KB，不中断流水线。
    similar = retrieve_similar(ticket)
    asset_ctx = get_asset_context(ticket)
    kb_ctx = get_kb_context(ticket)
    memory_ctx_str = format_memory_context(similar, asset_ctx, kb_ctx)

    # 先尝试拉取该工单对应资产的 IoT 实时遥测，作为证据化诊断的输入。
    # 任何失败（无对应传感器 / server 未起 / 超时）都回退 KB，不中断流水线。
    tool_calls: list[ToolCall] = []
    telemetry = None
    asset_id = providers._resolve_asset(ticket)
    if asset_id:
        try:
            res = providers.read_sensor_for_ticket(ticket)
            if res:
                telemetry = res
                tool_calls.append({
                    "agent": "diagnose",
                    "server": "iot",
                    "tool": "read_sensor",
                    "args": {"asset_id": asset_id, "metric": "all"},
                    "result": res,
                    "ts": datetime.now().isoformat(timespec="seconds"),
                })
        except Exception:  # noqa: BLE001
            telemetry = None

    root_cause = kb["root_cause"]
    recommended_action = kb["recommended_action"]
    confidence = 0.82 if not recurrence else 0.9
    ensemble_used = False

    if state.get("ensemble"):
        # Ensemble：扇出到多个可用模型，再用合成器整合
        avail = [c for c in get_ensemble_clients() if c.available]
        candidates = [c for c in (_single_diag(c, ticket, memory_ctx_str) for c in avail) if c]
        if candidates:
            ensemble_used = True
            if len(candidates) >= 2:
                synth = get_agent_client("diagnose")
                final = _synthesize(synth, candidates) if synth.available else None
                chosen = final or candidates[0]
            else:
                # 仅 1 个模型可用，退化为单模型（如实标注，不假装多模型协作）
                chosen = candidates[0]
            root_cause = chosen["root_cause"]
            recommended_action = chosen["recommended_action"]
            confidence = chosen["confidence"]
    else:
        # 单模型：使用本 Agent 绑定的模型（默认 deepseek）
        client = get_agent_client("diagnose")
        if client.available:
            parsed = _single_diag(client, ticket, memory_ctx_str)
            if parsed:
                root_cause = parsed["root_cause"]
                recommended_action = parsed["recommended_action"]
                confidence = parsed["confidence"]

    evidence: list[str] = []
    if telemetry:
        # 用真实传感器数据佐证根因，提升诊断可信度（"证据化诊断"）
        ev = _summarize_telemetry(telemetry)
        evidence.append(f"IoT({telemetry.get('asset_id')})：{ev}")
        root_cause = f"{root_cause}（IoT佐证：{ev}）"

    # 用持久化记忆佐证根因：同类型既往处置作为经验参考。
    if similar:
        top = similar[0]
        evidence.append(
            f"历史经验：同类型既往 {len(similar)} 条，典型根因「{top['root_cause']}」（{top['building']}）"
        )
        root_cause = (
            f"{root_cause}（历史佐证：同类型既往 {len(similar)} 次，"
            f"典型根因「{top['root_cause']}」）"
        )
        confidence = min(0.98, confidence + 0.03)  # 记忆增强：置信度小幅上浮

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
    extra = "（近期重复发生，已升级处置）" if recurrence else ""
    if ensemble_used:
        ens_tag = " [Ensemble]" if len([c for c in get_ensemble_clients() if c.available]) >= 2 else " [Ensemble·单模型]"
    else:
        ens_tag = ""
    mcp_note = f" + IoT遥测({telemetry['asset_id']})" if telemetry else ""
    mem_note = f" + 记忆({len(similar)})" if similar else ""
    log = (
        f"[Diagnose{ens_tag}{mcp_note}{mem_note}] 类型={ticket['type']} → 根因={root_cause}；"
        f"建议={recommended_action}；预估¥{kb['estimated_cost']:.0f}；"
        f"SLA {kb['sla_hours']}h；置信度={confidence:.2f}{extra}"
    )
    return {
        "diagnosis": diag,
        "tool_calls": tool_calls,
        "messages": [{"role": "system", "content": log}],
    }
