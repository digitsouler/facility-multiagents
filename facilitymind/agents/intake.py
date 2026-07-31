"""Intake Agent：工单受理与结构化。

职责：把业主/巡检的原始报修文本，转成系统可读的结构化工单。
有 LLM 时用模型抽取；无 LLM 时回退到关键词规则（保证离线可跑）。
"""
import logging
import re
from datetime import datetime

from ..knowledge import KB
from ..llm import extract_json, get_agent_client
from ..services import classify_fault
from ..state import FacilityState, Ticket
log = logging.getLogger("facilitymind.intake")


# 兼容 LLM 输出中文类型名时映射回系统 key
_TYPE_LABEL_MAP: dict[str, str] = {
    "电梯": "elevator",
    "空调": "hvac",
    "漏水": "leak",
    "照明": "lighting",
    "消防": "fire",
    "门禁": "access",
    "保洁": "cleaning",
    "绿化": "greening",
    "充电桩": "charging",
}



def _guess_location(text: str) -> str:
    """从文本里尽量抽取位置信息，如 'A座3#梯'、'B栋12楼'。"""
    m = re.search(r"[A-Za-z]?[一二三四五六七八九十栋座层楼号梯间]+[栋座层楼号梯间]?", text)
    return m.group(0) if m else "未指定位置"


def intake_agent(state: FacilityState) -> dict:
    raw = state["ticket"]["raw"]
    reporter = state["ticket"].get("reporter", "系统巡检")

    # 规则值作为默认，LLM 仅在解析有效时覆盖，保证任何情况下都有合法结论
    base = classify_fault(raw)
    ttype = base["type"]
    urgency = base["urgency"]
    client = get_agent_client("intake")
    if client.available:
        sys_prompt = (
            "你是物业工单受理助手。从报修文本中抽取：故障类型(key: elevator/hvac/leak/lighting/"
            "fire/access/cleaning/greening/charging)、紧急程度(high/medium/low)。只返回 JSON："
            '{"type": "...", "urgency": "..."}。'
        )
        out = client.complete(sys_prompt, raw)
        log.info("[Intake] LLM 输出=%s", out)
        parsed = extract_json(out)
        if parsed:
            cand_type = str(parsed.get("type", "")).strip().lower()
            cand_urg = str(parsed.get("urgency", "")).strip().lower()
            # 优先把中文标签映射回 key，其次匹配已有 key，否则保留规则值
            mapped = _TYPE_LABEL_MAP.get(cand_type, cand_type)
            if mapped in KB:
                ttype = mapped
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
    log1 = f"[Intake] 受理工单 {ticket['id']} → 类型={ttype}, 紧急度={urgency}, 位置={ticket['location']}"
    return {"ticket": ticket, "messages": [{"role": "system", "content": log1}]}
