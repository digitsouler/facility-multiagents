"""终端 HITL 演示：单条工单跑通三个人工节点，遇 interrupt 读入决策后续跑。

不依赖 Web/CLI 产品入口，仅开发演示用。
用法：python -m facilitymind.scripts.hitl_demo --id T-001
      （也可管道喂入：每行对应一次 input，按 approval→回传→复核 的顺序）
"""

import argparse
import os

from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from ..dataio import load_tickets
from ..graph import app
from ..logconf import setup_logging


def _prompt(payload: dict) -> dict:
    """把 interrupt 载荷转成一次人工决策；支持直接粘贴 JSON 或逐行输入。"""
    print("\n" + payload.get("prompt", "请决策"))
    raw = input("决策(JSON 或回车用默认): ").strip()
    if raw.startswith("{"):
        import json
        return json.loads(raw)
    if "plan" in payload:                      # Approval
        approved = input("批准?(y/n) [y]: ").strip().lower() != "n"
        approver = input("审批人 [现场主管]: ").strip() or "现场主管"
        note = input("备注: ").strip()
        return {"approved": approved, "approver": approver, "note": note}
    if "issues" in payload:                    # QA 复核
        reviewer = input("复核人 [质检主管]: ").strip() or "质检主管"
        note = input("复核备注: ").strip()
        return {"reviewer": reviewer, "review_note": note}
    technician = input("师傅/班组 [现场班组]: ").strip() or "现场班组"   # 师傅回传
    try:
        rm = int(input("实际响应分钟: ").strip() or "0")
    except ValueError:
        rm = 0
    photos = input("影像留痕?(y/n) [y]: ").strip().lower() != "n"
    cert = input("资质核验?(y/n) [y]: ").strip().lower() != "n"
    note = input("备注: ").strip()
    return {"technician": technician, "actual_response_min": rm,
            "photos_uploaded": photos, "cert_verified": cert, "completion_note": note}


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    setup_logging(log_file=os.path.join(root, "logs", "pipeline.log"))
    parser = argparse.ArgumentParser(description="FacilityMind 终端 HITL 演示")
    parser.add_argument("--id", required=True, help="工单 ID")
    args = parser.parse_args()

    ticket = next((t for t in load_tickets() if t["id"] == args.id), None)
    if not ticket:
        print(f"未找到工单 {args.id}")
        return

    thread = {"configurable": {"thread_id": "demo-" + ticket["id"]}}
    state = {"ticket": ticket, "auto_approve": False}
    resume = None
    while True:
        try:
            if resume is None:
                result = app.invoke(state, thread)
            else:
                result = app.invoke(Command(resume=resume), thread)
            break  # 没有 interrupt，跑完
        except GraphInterrupt as gi:
            payload = gi.args[0] if gi.args else {}
            resume = _prompt(payload)

    qa = result.get("qa", {})
    print("\n==== 结案 ====")
    print(result.get("report", {}).get("summary", ""))
    print(f"质检通过={qa.get('passed')} 得分={qa.get('score')} 复核人={qa.get('reviewer')}")


if __name__ == "__main__":
    main()
