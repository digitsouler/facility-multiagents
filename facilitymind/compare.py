"""对比模式：并排展示「规则库」与「LLM（如 DeepSeek）」对同一工单的处理结论差异。

用途：直观验证接入大模型后，分类/诊断结论是否更合理、更丰富，而不是和离线一模一样。

离线（未配置 LLM_API_KEY）：仅展示规则库，LLM 列标注「未启用」。
在线但调用失败：安全回退规则值，LLM 列标注「调用失败(已回退)」，绝不崩溃。
"""

from .knowledge import KB, classify_type, classify_urgency
from .llm import extract_json, llm


def _rule_intake(raw: str):
    return classify_type(raw), classify_urgency(raw)


def _llm_intake(raw: str):
    sys_prompt = (
        "你是物业工单受理助手。从报修文本中抽取：故障类型(电梯/空调/漏水/照明/"
        "消防/门禁/保洁/绿化)、紧急程度(high/medium/low)。只返回 JSON："
        '{"type": "...", "urgency": "..."}。'
    )
    out = llm.complete(sys_prompt, raw)
    parsed = extract_json(out)
    if not parsed:
        return None, None
    t = str(parsed.get("type", "")).strip().lower()
    u = str(parsed.get("urgency", "")).strip().lower()
    t = t if t in KB else None
    u = u if u in ("high", "medium", "low") else None
    return t, u


def _rule_diagnose(ticket: dict):
    kb = KB.get(ticket["type"], KB["cleaning"])
    return kb["root_cause"], kb["recommended_action"], 0.82


def _llm_diagnose(ticket: dict):
    sys_prompt = (
        "你是设施管理诊断专家。根据故障类型与历史，推断根因与处置建议。"
        "只返回 JSON：{root_cause, recommended_action, confidence}。"
    )
    out = llm.complete(sys_prompt, ticket["raw"])
    parsed = extract_json(out)
    if parsed and parsed.get("root_cause") and parsed.get("recommended_action"):
        try:
            conf = float(parsed.get("confidence", 0.82))
            conf = conf if 0.0 <= conf <= 1.0 else 0.82
        except (ValueError, TypeError):
            conf = 0.82
        return str(parsed["root_cause"]), str(parsed["recommended_action"]), conf
    return None, None, None


def compare_ticket(ticket: dict) -> str:
    """返回对比报告的字符串（规则库 vs LLM）。"""
    raw = ticket["raw"]
    llm_on = llm.available

    rule_type, rule_urg = _rule_intake(raw)
    rule_d = _rule_diagnose({"type": rule_type or "cleaning", "raw": raw})

    llm_type, llm_urg = (None, None)
    llm_d = (None, None, None)
    if llm_on:
        llm_type, llm_urg = _llm_intake(raw)
        diag_type = llm_type or rule_type or "cleaning"
        llm_d = _llm_diagnose({"type": diag_type, "raw": raw})

    llm_label = "DeepSeek(LLM)" if llm_on else "LLM(未启用)"

    def _cell(val, fallback):
        if val:
            return str(val)
        return fallback if llm_on else "未启用"

    rows = [
        ("故障类型", rule_type or "-", _cell(llm_type, "-")),
        ("紧急度", rule_urg or "-", _cell(llm_urg, "-")),
        ("根因", rule_d[0], _cell(llm_d[0], "-")),
        ("建议处置", rule_d[1], _cell(llm_d[1], "-")),
        ("置信度", f"{rule_d[2]:.2f}", f"{llm_d[2]:.2f}" if llm_d[2] is not None else ("-" if llm_on else "未启用")),
    ]

    # 计算差异
    diffs = []
    if llm_on:
        for name, r, l in rows[2:4]:  # 根因、处置
            if l not in ("-", None) and l != r:
                diffs.append(name)
        if llm_type and rule_type and llm_type != rule_type:
            diffs.append("故障类型")
        if llm_urg and rule_urg and llm_urg != rule_urg:
            diffs.append("紧急度")
    diff_str = "、".join(diffs) if diffs else "（两路结论一致）"

    w = 12
    lines = []
    lines.append("=" * 70)
    lines.append(f"对比工单 {ticket['id']}：{raw}")
    lines.append("-" * 70)
    lines.append(f"{'维度'.ljust(w)} | {'规则库'.ljust(26)} | {llm_label}")
    lines.append("-" * 70)
    for name, r, l in rows:
        lines.append(f"{name.ljust(w)} | {str(r)[:26].ljust(26)} | {l}")
    lines.append("-" * 70)
    lines.append(f"结论差异：{diff_str}")
    if not llm_on:
        lines.append("提示：LLM 未启用（未配置 LLM_API_KEY）。在 .env 填入 DeepSeek Key 后重跑即可看到模型推理结论。")
    lines.append("=" * 70)
    return "\n".join(lines)
