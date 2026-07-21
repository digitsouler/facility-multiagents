"""对比模式：并排展示不同模型对同一工单的结论差异。

两种用法：
- compare_ticket：规则库 vs 默认 LLM（如 DeepSeek）的结论差异（旧行为）。
- compare_models：规则库 vs 全部已启用模型（DeepSeek / Qwen / ...）的横向对比，
  直观看多模型协作下各模型在分类/诊断上的分歧与一致性。

离线（未配置任何 Key）：仅有规则库一列；LLM 列标注「未启用」。
在线但调用失败：安全回退规则值，绝不崩溃。
"""

from .knowledge import KB, classify_type, classify_urgency
from .llm import extract_json, get_client, list_models, llm


def _rule_intake(raw: str):
    return classify_type(raw), classify_urgency(raw)


def _model_intake(client, raw: str):
    sys_prompt = (
        "你是物业工单受理助手。从报修文本中抽取：故障类型(电梯/空调/漏水/照明/"
        "消防/门禁/保洁/绿化)、紧急程度(high/medium/low)。只返回 JSON："
        '{"type": "...", "urgency": "..."}。'
    )
    out = client.complete(sys_prompt, raw)
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


def _model_diagnose(client, ticket: dict):
    sys_prompt = (
        "你是设施管理诊断专家。根据故障类型与历史，推断根因与处置建议。"
        "只返回 JSON：{root_cause, recommended_action, confidence}。"
    )
    out = client.complete(sys_prompt, ticket["raw"])
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
    """规则库 vs 默认 LLM（DeepSeek）的单模型对比。"""
    raw = ticket["raw"]
    llm_on = llm.available

    rule_type, rule_urg = _rule_intake(raw)
    rule_d = _rule_diagnose({"type": rule_type or "cleaning", "raw": raw})

    llm_type, llm_urg = (None, None)
    llm_d = (None, None, None)
    if llm_on:
        llm_type, llm_urg = _model_intake(llm, raw)
        diag_type = llm_type or rule_type or "cleaning"
        llm_d = _model_diagnose(llm, {"type": diag_type, "raw": raw})

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

    diffs = []
    if llm_on:
        for name, r, l in rows[2:4]:
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


def compare_models(ticket: dict) -> str:
    """规则库 vs 全部已启用模型（DeepSeek / Qwen / ...）的多模型横向对比。"""
    raw = ticket["raw"]
    rule_type, rule_urg = _rule_intake(raw)
    rule_d = _rule_diagnose({"type": rule_type or "cleaning", "raw": raw})

    cols = [("规则库", {
        "type": rule_type, "urgency": rule_urg,
        "root_cause": rule_d[0], "recommended_action": rule_d[1], "confidence": rule_d[2],
    })]

    for m in list_models():
        if not m["available"]:
            continue
        c = get_client(m["name"])
        t, u = _model_intake(c, raw)
        t = t or rule_type or "cleaning"
        rc, ra, conf = _model_diagnose(c, {"type": t, "raw": raw})
        cols.append((m["label"], {
            "type": t, "urgency": u,
            "root_cause": rc, "recommended_action": ra, "confidence": conf,
        }))

    dims = [("故障类型", "type"), ("紧急度", "urgency"), ("根因", "root_cause"),
            ("建议处置", "recommended_action"), ("置信度", "confidence")]
    w = 10
    lines = []
    lines.append("=" * 96)
    lines.append(f"多模型对比 工单 {ticket['id']}：{raw}")
    lines.append("-" * 96)
    lines.append("维度".ljust(w) + " | " + " | ".join(c[0][:12].ljust(12) for c in cols))
    lines.append("-" * 96)
    for name, key in dims:
        cells = []
        for _, vals in cols:
            v = vals.get(key)
            if key == "confidence":
                cells.append(f"{v:.2f}" if isinstance(v, float) else str(v))
            else:
                cells.append(str(v) if v else "-")
        lines.append(name.ljust(w) + " | " + " | ".join(c[:12].ljust(12) for c in cells))
    lines.append("=" * 96)
    if len(cols) == 1:
        lines.append("提示：当前未启用任何模型（无 API Key）。配置 QWEN_API_KEY 等后，这里会出现多列模型对比。")
    return "\n".join(lines)
