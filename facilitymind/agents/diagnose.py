"""Diagnose Agent：根因诊断与处置建议。

职责：结合故障类型知识库 + 历史工单，给出根因、建议动作、所需技能、预估成本、SLA。

在线模式默认采用 **ReAct（推理-行动-观察）自主诊断**：诊断智能体自行决定
「先查历史记忆还是先拉 IoT 传感器」，观察返回后再推理，直到证据充分才结案。
每轮推理（LLM）与工具调用（记忆检索 / IoT 传感器）都会作为 trace 子 span 嵌套进
Diagnose，形成可回溯的「思考树」，前端执行链路时间线直接可视化。

有 LLM 时让模型做推理；无 LLM（或显式关闭 react）时走确定性诊断：直接拉取
记忆 + IoT 遥测，再用 KB / 单模型 / Ensemble 出结论。开启 ensemble 时扇出多模型整合。
"""

import json
from datetime import datetime

from ..dataio import load_tickets
from ..knowledge import KB
from ..llm import extract_json, get_agent_client, get_ensemble_clients
from ..mcp import providers
from ..memory.retrieval import retrieve_similar, get_asset_context, get_kb_context, format_memory_context
from ..state import Diagnosis, FacilityState, ToolCall
from ..tracer import span, _trunc

REACT_MAX_STEPS = 4


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


def _gather_memory(ticket):
    """检索历史记忆，返回 (similar, asset_ctx, kb_ctx, ctx_str, obs_text)。"""
    similar = retrieve_similar(ticket)
    asset_ctx = get_asset_context(ticket)
    kb_ctx = get_kb_context(ticket)
    ctx_str = format_memory_context(similar, asset_ctx, kb_ctx)
    if similar:
        top = similar[0]
        obs = f"检索到 {len(similar)} 条相似历史，典型根因「{top['root_cause']}」（{top['building']}）"
    elif asset_ctx:
        obs = f"资产档案：{asset_ctx['asset_id']}，优选供应商 {asset_ctx['preferred_vendor']}"
    elif kb_ctx:
        obs = f"沉淀知识 {len(kb_ctx)} 条"
    else:
        obs = "无相关历史记忆"
    return similar, asset_ctx, kb_ctx, ctx_str, obs


def _gather_sensor(ticket):
    """读取 IoT 实时遥测，返回 (telemetry|None, tool_call|None, obs_text)。"""
    asset_id = providers._resolve_asset(ticket)
    if not asset_id:
        return None, None, "该工单无对应受监控资产，跳过传感器"
    try:
        res = providers.read_sensor_for_ticket(ticket)
    except Exception:  # noqa: BLE001
        return None, None, "读取传感器异常"
    if not res:
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
    return res, tc, f"IoT({res.get('asset_id')})：{ev}"


def _react_step(client, ticket: dict, scratchpad: str) -> dict | None:
    """ReAct 一轮：让模型基于「工单 + 已收集证据」决定下一步 action。

    返回解析出的 JSON dict；解析失败或非法动作返回 None（调用方据此提前结案）。
    """
    sys_prompt = (
        "你是设施管理诊断智能体，采用 ReAct（推理-行动-观察）范式自主诊断故障。\n"
        "可用工具：\n"
        "- recall：检索历史故障记忆（相似事件 / 资产档案 / 沉淀知识）。需要历史经验时调用。\n"
        "- sensor：读取该工单对应资产的 IoT 实时遥测（温度/流量/电流等异常指标）。"
        "故障涉及设备运行数据时调用。\n"
        "每轮严格返回 JSON：\n"
        '{"thought":"你的推理","action":"recall"|"sensor"|"finish","argument":"本次工具调用的目的"}。\n'
        '当 action="finish" 时额外返回 {"root_cause":"...","recommended_action":"...","confidence":0.0~1.0}。\n'
        "规则：先想「还缺什么证据」再决定调用哪个工具；证据充分（recall 与 sensor 均已调用或确认无需）"
        "后立即 finish 输出最终诊断；最多 4 轮；不要编造工具返回，只能基于 observation 推理。"
    )
    user = f"工单：{ticket['raw']}\n\n" + (scratchpad if scratchpad else "尚无已收集证据，请开始诊断。")
    out = client.complete(sys_prompt, user)
    return extract_json(out)


def _react_diagnose(client, ticket: dict, ensemble: bool) -> dict:
    """ReAct 自主诊断主循环（在线模式）。

    每轮：LLM 推理 → 决定 action（recall/sensor/finish）→ 执行工具 → 观察 → 下一轮。
    每轮作为一个 trace span，其内部的 LLM 调用与工具调用自动嵌套为子 span。
    返回与 _deterministic_diagnose 同构的字典。
    """
    similar, asset_ctx, kb_ctx, memory_ctx_str = [], None, [], ""
    telemetry = None
    tool_calls: list[ToolCall] = []
    ensemble_used = False
    final = None
    scratchpad = ""
    used: dict[str, str] = {}  # action -> 已返回的 observation，避免重复调用同一工具浪费轮次

    for i in range(1, REACT_MAX_STEPS + 1):
        with span(f"ReAct 迭代 {i}", "agent") as rs:
            parsed = _react_step(client, ticket, scratchpad)
            action = str(parsed.get("action", "")).lower() if parsed else ""
            thought = parsed.get("thought", "") if parsed else ""
            if rs:
                rs.input_brief = _trunc(thought, 50)
            if action not in ("recall", "sensor", "finish"):
                if rs:
                    rs.finish(output_brief="动作解析失败，转结案")
                break
            if action in used:
                # 同一工具已调用过，复用结果并提示模型转向其他工具或结案
                obs = used[action]
                hint = "（该工具已调用过，结果同上；若证据充分请 finish，否则调用另一工具）"
                scratchpad += f"\n回合{i}｜思考：{thought}\n[{action}] {obs}{hint}\n"
                if rs:
                    rs.finish(output_brief=_trunc(obs, 50))
                continue
            if action == "recall":
                similar, asset_ctx, kb_ctx, memory_ctx_str, obs = _gather_memory(ticket)
                used["recall"] = obs
                scratchpad += f"\n回合{i}｜思考：{thought}\n[recall] {obs}\n"
                if rs:
                    rs.finish(output_brief=_trunc(obs, 50))
            elif action == "sensor":
                tel, tc, obs = _gather_sensor(ticket)
                if tel:
                    telemetry = tel
                if tc:
                    tool_calls.append(tc)
                used["sensor"] = obs
                scratchpad += f"\n回合{i}｜思考：{thought}\n[sensor] {obs}\n"
                if rs:
                    rs.finish(output_brief=_trunc(obs, 50))
            else:  # finish
                rc = parsed.get("root_cause")
                ra = parsed.get("recommended_action")
                try:
                    conf = float(parsed.get("confidence", 0.82))
                except (ValueError, TypeError):
                    conf = 0.82
                if rc and ra:
                    final = {"root_cause": str(rc), "recommended_action": str(ra), "confidence": conf}
                scratchpad += f"\n回合{i}｜思考：{thought}\n[finish] 诊断完成。\n"
                if rs:
                    rs.finish(output_brief=_trunc(str(rc), 50))
                break

    steps = i  # 实际执行的迭代轮数

    # 若未正常 finish（达上限 / 解析失败），用已收集证据让模型直接出诊断
    if final is None:
        if memory_ctx_str:
            d = _single_diag(client, ticket, memory_ctx_str)
            if d:
                final = d
        if final is None:
            kb = KB.get(ticket["type"], KB["cleaning"])
            final = {
                "root_cause": kb["root_cause"],
                "recommended_action": kb["recommended_action"],
                "confidence": 0.82,
            }

    root_cause = final["root_cause"]
    recommended_action = final["recommended_action"]
    confidence = final["confidence"]

    # Ensemble：在已收集证据基础上扇出多模型整合最终诊断
    if ensemble:
        avail = [c for c in get_ensemble_clients() if c.available]
        if len(avail) >= 2:
            candidates = [c for c in (_single_diag(c, ticket, memory_ctx_str) for c in avail) if c]
            if candidates:
                ensemble_used = True
                synth = get_agent_client("diagnose")
                chosen = _synthesize(synth, candidates) if synth.available else None
                chosen = chosen or candidates[0]
                root_cause = chosen["root_cause"]
                recommended_action = chosen["recommended_action"]
                confidence = chosen["confidence"]

    return {
        "root_cause": root_cause,
        "recommended_action": recommended_action,
        "confidence": confidence,
        "telemetry": telemetry,
        "similar": similar,
        "tool_calls": tool_calls,
        "ensemble_used": ensemble_used,
        "react_steps": steps,
    }


def _deterministic_diagnose(ticket: dict, recurrence: bool, ensemble: bool) -> dict:
    """确定性诊断：直接拉取记忆 + IoT 遥测，再用 KB / 单模型 / Ensemble 出结论。

    用于无 LLM（离线）或显式关闭 react 的场景；ReAct 不可用时自动回退到这里。
    """
    similar, asset_ctx, kb_ctx, memory_ctx_str, _obs = _gather_memory(ticket)
    telemetry = None
    tool_calls: list[ToolCall] = []
    tel, tc, _obs2 = _gather_sensor(ticket)
    if tel:
        telemetry = tel
    if tc:
        tool_calls.append(tc)

    kb = KB.get(ticket["type"], KB["cleaning"])
    root_cause = kb["root_cause"]
    recommended_action = kb["recommended_action"]
    confidence = 0.9 if recurrence else 0.82
    ensemble_used = False

    if ensemble:
        avail = [c for c in get_ensemble_clients() if c.available]
        candidates = [c for c in (_single_diag(c, ticket, memory_ctx_str) for c in avail) if c]
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
        client = get_agent_client("diagnose")
        if client.available:
            parsed = _single_diag(client, ticket, memory_ctx_str)
            if parsed:
                root_cause = parsed["root_cause"]
                recommended_action = parsed["recommended_action"]
                confidence = parsed["confidence"]

    return {
        "root_cause": root_cause,
        "recommended_action": recommended_action,
        "confidence": confidence,
        "telemetry": telemetry,
        "similar": similar,
        "tool_calls": tool_calls,
        "ensemble_used": ensemble_used,
        "react_steps": 0,
    }


def diagnose_agent(state: FacilityState) -> dict:
    ticket = state["ticket"]
    kb = KB.get(ticket["type"], KB["cleaning"])
    recurrence = _check_recurrence(ticket)
    ensemble = bool(state.get("ensemble"))
    use_react = bool(state.get("react", True))

    client = get_agent_client("diagnose")
    if client.available and use_react:
        res = _react_diagnose(client, ticket, ensemble)
    else:
        res = _deterministic_diagnose(ticket, recurrence, ensemble)

    root_cause = res["root_cause"]
    recommended_action = res["recommended_action"]
    confidence = res["confidence"]
    telemetry = res["telemetry"]
    similar = res["similar"]
    tool_calls = res["tool_calls"]
    ensemble_used = res["ensemble_used"]
    react_steps = res["react_steps"]

    # ---- 公共收尾：证据佐证 + 构建 Diagnosis + 日志 ----
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
    react_note = f" · ReAct×{react_steps}" if react_steps else ""
    mcp_note = f" + IoT遥测({telemetry['asset_id']})" if telemetry else ""
    mem_note = f" + 记忆({len(similar)})" if similar else ""
    log = (
        f"[Diagnose{ens_tag}{react_note}{mcp_note}{mem_note}] 类型={ticket['type']} → 根因={root_cause}；"
        f"建议={recommended_action}；预估¥{kb['estimated_cost']:.0f}；"
        f"SLA {kb['sla_hours']}h；置信度={confidence:.2f}{extra}"
    )
    return {
        "diagnosis": diag,
        "tool_calls": tool_calls,
        "messages": [{"role": "system", "content": log}],
    }
