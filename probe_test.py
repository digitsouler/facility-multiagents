"""独立验证 probe 自适应 ReAct 分支：用一份「证据矛盾」的工单跑诊断图，
观察综合诊断置信度 < 0.7 时是否进入 probe 子循环。
"""
import logging
from langchain_core.messages import HumanMessage
from facilitymind.agents.diagnose import _DIAG_GRAPH, _diag_user

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

# 证据矛盾工单：业主报修异常，但传感器/KB/案例三方读数互相打架
TICKET = {
    "id": "T-PROBE",
    "raw": "业主报修：地下车库照明全部不亮，疑似线路烧毁。\n"
           "但 IoT 传感器实时读数显示配电箱电压正常、无跳闸；\n"
           "知识库无对应照明故障记录；历史案例库为空。证据互相矛盾，无法确认根因。",
    "type": "lighting",
    "urgency": "high",
    "location": "B2车库",
    "location_hint": "B2车库",
    "reporter": "业主王女士",
    "created_at": "2026-07-31",
}

init = {
    "ticket": TICKET,
    "messages": [HumanMessage(content=_diag_user(TICKET, []))],
    "confidence": 1.0,
    "probe_iteration": 0,
}

res = _DIAG_GRAPH.invoke(init)
conf = res.get("confidence", 1.0)
entered_probe = res.get("__interrupt__") is not None or "probe" in [
    e for e in []  # placeholder; 实际看日志
]
print("\n===== 验证结果 =====")
print(f"综合诊断置信度 = {conf:.2f}")
print(f"是否进入 probe（conf<0.7）? {'是 ✅' if conf < 0.7 else '否（直接 END）'}")
print("（日志见上方 [Diagnose][hybrid] 综合诊断 置信度=...）")
