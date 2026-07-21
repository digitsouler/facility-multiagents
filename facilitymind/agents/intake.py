"""Intake Agent：工单受理与结构化。

职责：把业主/巡检的原始报修文本，转成系统可读的结构化工单。
有 LLM 时用模型抽取；无 LLM 时回退到关键词规则（保证离线可跑）。
"""

import re
from datetime import datetime

from ..knowledge import KB, classify_type, classify_urgency
from ..llm import extract_json, get_agent_client
from ..state import FacilityState, Ticket


def _guess_location(text: str) -> str:
    """从文本里尽量抽取位置信息，如 'A座3#梯'、'B栋12楼'。"""
    m = re.search(r"[A-Za-z]?[一二三四五六七八九十栋座层楼号梯间]+[栋座层楼号梯间]?", text)
    return m.group(0) if m else "未指定位置"


def intake_agent(state: FacilityState) -> dict:
    raw = state["ticket"]["raw"]
    reporter = state["ticket"].get("reporter", "系统巡检")

    # 规则值作为默认，LLM 仅在解析有效时覆盖，保证任何情况下都有合法结论
    ttype = classify_type(raw)
    urgency = classify_urgency(raw)
    client = get_agent_client("intake")
    if client.available:
        sys_prompt = (
            "你是物业工单受理助手。从报修文本中抽取：故障类型(电梯/空调/漏水/照明/"
            "消防/门禁/保洁/绿化)、紧急程度(high/medium/low)。只返回 JSON："
            '{"type": "...", "urgency": "..."}。'
        )
        out = client.complete(sys_prompt, raw)
        parsed = extract_json(out)
        if parsed:
            cand_type = str(parsed.get("type", "")).strip().lower()
            cand_urg = str(parsed.get("urgency", "")).strip().lower()
            # 只在模型输出落在合法枚举内才采纳，否则保留规则值，避免脏数据
            if cand_type in KB:
                ttype = cand_type
            if cand_urg in ("high", "medium", "low"):
                urgency = cand_urg

    location = state["ticket"].get("location_hint") or _guess_location(raw)
    ticket: Ticket = {
        "id": state["ticket"].get("id", "T-0000"),
        "raw": raw,
        "type": ttype,
        "urgency": urgency,
        "location": location,
        "location_hint": state["ticket"].get("location_hint", ""),
        "reporter": reporter,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    log = f"[Intake] 受理工单 {ticket['id']} → 类型={ttype}, 紧急度={urgency}, 位置={ticket['location']}"
    return {"ticket": ticket, "messages": [{"role": "system", "content": log}]}
