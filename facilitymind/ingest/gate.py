"""意图闸门

判断一段文本是否属于「维修工单」场景；非维修类（投诉/能耗/闲聊等）一律挡在门外，
不进入后续 Intake / Diagnose 流程。判定以规则为主、保证离线可跑，可选接 LLM 增强。

说明：这是与 Intake 不同层的路由——
- 本闸门 = 场景级（维修 / 非维修），在 Intake 之前；
- Intake = 故障类型级（电梯/空调/消防/清洁…），在闸门放行之后。
"""
import logging

log = logging.getLogger("facilitymind.ingest.gate")

# 维修类关键词：命中即视为报修
_MAINT_KEYWORDS = [
    "电梯", "空调", "漏水", "照明", "灯", "消防", "门禁", "保洁", "绿化", "充电桩",
    "报修", "故障", "坏了", "不工作", "不通", "不亮", "卡住", "异响", "过热",
    "维修", "维保", "检修", "停水", "停电", "跳闸", "堵塞", "关不上", "不制冷",
]
# 明确非维修场景（命中且不含维修词时，直接「不支持」）
_NON_MAINT_HINTS = ["投诉", "抱怨", "电费", "能耗", "节能", "保养计划", "巡检计划"]
# 闲聊/问答信号：不含维修词时视为非工单
_QUESTION_LIKE = ["?", "？", "怎么", "为什么", "如何", "多少", "天气", "你好"]


def route_intent(text: str) -> dict:
    """返回 {"scene": "maintenance"|"unknown", "is_ticket": bool, "reason": str}。"""
    if not text or not text.strip():
        return {"scene": "unknown", "is_ticket": False, "reason": "空文本，无法识别为工单"}

    # 闲聊 / 问答：明显不是报修
    if any(q in text for q in _QUESTION_LIKE) and not any(k in text for k in _MAINT_KEYWORDS):
        return {"scene": "unknown", "is_ticket": False, "reason": "看起来是闲聊/问答，非报修内容"}

    # 明确非维修场景
    for h in _NON_MAINT_HINTS:
        if h in text and not any(k in text for k in _MAINT_KEYWORDS):
            return {"scene": "unknown", "is_ticket": False, "reason": f"命中非维修场景关键词「{h}」，当前仅支持维修工单"}

    # 维修类关键词命中
    hit = [k for k in _MAINT_KEYWORDS if k in text]
    if hit:
        return {"scene": "maintenance", "is_ticket": True, "reason": f"命中维修关键词：{', '.join(hit)}"}

    return {"scene": "unknown", "is_ticket": False, "reason": "未命中任何维修关键词，无法判定为工单"}
