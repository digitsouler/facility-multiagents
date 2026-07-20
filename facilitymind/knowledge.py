"""领域知识库与规则引擎（离线可跑的核心）。

这一部分把"物业设施管理经验"固化为结构化知识：
- TYPE_KEYWORDS：报修文本 → 故障类型的映射（Intake Agent 用）
- URGENCY_KEYWORDS：高紧急度关键词（困人、冒烟、漏水等）
- KB：每种故障类型的典型根因、建议动作、所需技能、预估成本、SLA
- VENDORS：可调度资源池（技能标签、响应时间、报价）

真实生产中，这些知识可来自 CMMS 历史工单 + RAG 知识库；MVP 先用规则库保证可复现。
"""

from .state import Ticket

TYPE_KEYWORDS: dict[str, list[str]] = {
    "elevator": ["电梯", "升降", "轿厢", "梯"],
    "hvac": ["空调", "制冷", "冷气", "新风", "风机盘管"],
    "leak": ["漏水", "渗水", "水管", "爆管", "积水"],
    "lighting": ["灯", "照明", "断电", "跳闸", "停电"],
    "fire": ["消防", "烟感", "报警", "灭火器", "喷淋"],
    "access": ["门禁", "闸机", "道闸", "刷卡", "门打不开"],
    "cleaning": ["保洁", "垃圾", "污渍", "异味"],
    "greening": ["绿化", "草坪", "树木", "花草", "枯死"],
}

URGENCY_KEYWORDS_HIGH: list[str] = [
    "困人", "夹人", "冒烟", "起火", "火", "漏电", "积水", "爆管", "漏水", "渗水", "伤人", "报警", "停梯"
]
URGENCY_KEYWORDS_LOW: list[str] = ["轻微", "偶尔", "美观", "建议", "咨询", "问"]

# 故障类型 → 处置知识
KB: dict[str, dict] = {
    "elevator": {
        "root_cause": "门机控制器接触不良或光幕遮挡",
        "recommended_action": "更换门机控制器并校准光幕",
        "required_skill": "elevator_maint",
        "estimated_cost": 2400.0,
        "sla_hours": 2,
    },
    "hvac": {
        "root_cause": "滤网堵塞或冷媒不足",
        "recommended_action": "清洗滤网并补充冷媒",
        "required_skill": "hvac_maint",
        "estimated_cost": 800.0,
        "sla_hours": 6,
    },
    "leak": {
        "root_cause": "管道接口密封老化",
        "recommended_action": "更换密封件并做打压测试",
        "required_skill": "plumbing",
        "estimated_cost": 600.0,
        "sla_hours": 4,
    },
    "lighting": {
        "root_cause": "灯具损坏或空开跳闸",
        "recommended_action": "更换灯具或复位空开",
        "required_skill": "electrician",
        "estimated_cost": 300.0,
        "sla_hours": 4,
    },
    "fire": {
        "root_cause": "烟感探测器误报或电池欠压",
        "recommended_action": "现场确认并更换探测器电池",
        "required_skill": "fire_safety",
        "estimated_cost": 200.0,
        "sla_hours": 1,
    },
    "access": {
        "root_cause": "读卡器通讯故障或权限失效",
        "recommended_action": "重启读卡器并刷新权限",
        "required_skill": "access_ctrl",
        "estimated_cost": 400.0,
        "sla_hours": 3,
    },
    "cleaning": {
        "root_cause": "保洁排班遗漏或物料不足",
        "recommended_action": "补派保洁并补充物料",
        "required_skill": "cleaning",
        "estimated_cost": 150.0,
        "sla_hours": 8,
    },
    "greening": {
        "root_cause": "灌溉不足或病虫害",
        "recommended_action": "调整灌溉并施药",
        "required_skill": "landscape",
        "estimated_cost": 250.0,
        "sla_hours": 24,
    },
}

# 资源池：技能标签匹配的 vendor
VENDORS: list[dict] = [
    {"name": "迅达电梯维保", "skill": "elevator_maint", "response_min": 30, "cost": 2400.0},
    {"name": "美的暖通服务", "skill": "hvac_maint", "response_min": 45, "cost": 800.0},
    {"name": "广深管道抢修", "skill": "plumbing", "response_min": 40, "cost": 600.0},
    {"name": "珠江电气工程", "skill": "electrician", "response_min": 35, "cost": 300.0},
    {"name": "粤安消防维保", "skill": "fire_safety", "response_min": 20, "cost": 200.0},
    {"name": "智城门禁运维", "skill": "access_ctrl", "response_min": 50, "cost": 400.0},
    {"name": "净美保洁", "skill": "cleaning", "response_min": 60, "cost": 150.0},
    {"name": "园丁绿化", "skill": "landscape", "response_min": 120, "cost": 250.0},
]

# 人工确认阈值：派单报价超过该金额需要人工审批（企业落地常见管控点）。
APPROVAL_THRESHOLD_COST: float = 2000.0

# 各类故障的 QA 检查清单（合规/安全关键点）。
# QA Agent 会据此逐项核验，资质类条目依赖资质核验、影像类条目依赖留痕。
QA_CHECKLISTS: dict[str, list[str]] = {
    "elevator": ["核验特种设备作业人员证", "设置围挡与警示标志", "光幕重新校准并测试", "困人应急流程复核"],
    "hvac": ["断电挂牌后作业", "冷媒回收合规处置", "出风温度复测达标"],
    "leak": ["关闭上游阀门并泄压", "维修后打压测试合格", "留存渗漏点位照片"],
    "lighting": ["断开上级电源并挂牌", "测试绝缘电阻合格"],
    "fire": ["现场确认无火情后复位", "探测器灵敏度测试", "台账记录闭环"],
    "access": ["权限变更留痕审计", "读卡器联动测试"],
    "cleaning": ["作业区域围挡提示", "污渍前后对比留痕"],
    "greening": ["用药安全告知", "灌溉水量记录"],
}


def classify_type(text: str) -> str:
    """根据关键词把报修文本归类为故障类型。"""
    for t, kws in TYPE_KEYWORDS.items():
        if any(kw in text for kw in kws):
            return t
    return "cleaning"  # 兜底


def classify_urgency(text: str) -> str:
    if any(kw in text for kw in URGENCY_KEYWORDS_HIGH):
        return "high"
    if any(kw in text for kw in URGENCY_KEYWORDS_LOW):
        return "low"
    return "medium"
