"""命令行入口：加载合成工单，端到端跑通 FacilityMind 工作流。

用法：
  python -m facilitymind.cli --id T-001        # 单条工单（人工确认节点会触发）
  python -m facilitymind.cli --all             # 跑全部示例工单（自动批准）
  python -m facilitymind.cli --scenario elevator_fault   # 跑预设场景
  python -m facilitymind.cli --id T-001 --auto # 跳过人工确认
  python -m facilitymind.cli --id T-001 --compare # 对比规则库 vs DeepSeek 结论差异

无 LLM Key 时自动走规则模式，开箱即跑。

说明：Approval 节点在派单成本超阈值时会调用 LangGraph 的 interrupt() 暂停工作流。
若运行在交互终端（stdin 为 TTY），会提示人工批准/驳回；若非交互环境（如 CI/脚本），
为避免阻塞将默认自动批准，并打印触发标记，方便验证 HITL 逻辑已生效。
"""

import argparse
import json
import sys

from langgraph.types import Command

from .dataio import load_tickets
from .graph import app


def _print_result(ticket_id: str, result: dict) -> None:
    t = result.get("ticket", {})
    d = result.get("diagnosis", {})
    p = result.get("dispatch_plan", {})
    a = result.get("approval", {})
    e = result.get("execution", {})
    qa = result.get("qa", {})
    r = result.get("report", {})
    print("=" * 68)
    print(f"工单 {ticket_id} · {t.get('type')} · 紧急度={t.get('urgency')} · 位置={t.get('location')}")
    print("-" * 68)
    print(f"诊断根因 : {d.get('root_cause')}")
    print(f"建议处置 : {d.get('recommended_action')}")
    print(f"预估成本 : ¥{d.get('estimated_cost', 0):.0f}    SLA : {d.get('sla_hours')}h    置信度 : {d.get('confidence', 0):.2f}")
    if d.get("recurrence"):
        print("⚠ 近期重复发生，已升级处置")
    print(f"派单方案 : {p.get('vendor')}（响应 {p.get('response_time_min')} 分钟 / 报价 ¥{p.get('cost', 0):.0f}）")
    print(f"         {p.get('rationale')}")
    print(f"人工确认 : [{a.get('status')}] 批准={a.get('approved')} 审批人={a.get('approver')} — {a.get('note')}")
    print(f"实际响应 : {e.get('actual_response_min')} 分钟 | 资质核验={e.get('cert_verified')} | 影像留痕={e.get('photos_uploaded')}")
    print(f"质检结果 : {'通过' if qa.get('passed') else '未通过'}  得分={qa.get('score')}  问题={qa.get('issues') or '无'}")
    if r:
        print(f"结案摘要 : {r.get('summary')}")
        for rec in r.get("recommendations", []):
            print(f"   建议 · {rec}")
    print("=" * 68)
    print()


def run_one(raw_ticket: dict, config: dict, auto_approve: bool) -> dict:
    """运行单条工单；如需人工确认则处理 interrupt 并恢复。"""
    initial = {"ticket": raw_ticket, "auto_approve": auto_approve}
    result = app.invoke(initial, config)

    if "__interrupt__" in result:
        interrupts = result["__interrupt__"]
        payload = interrupts[0].value
        print("\n🔔 [HITL] 触发人工确认节点：", payload.get("prompt"))
        # 交互终端下提示人工批准/驳回；无法读取输入（CI/脚本环境）则自动批准，避免阻塞。
        try:
            ans = input("   批准该派单？(y/N): ").strip().lower()
            approved = ans in ("y", "yes", "是")
            note = input("   备注（可选，回车跳过）: ").strip()
            decision = {"approved": approved, "note": note, "approver": "现场主管"}
        except EOFError:
            print("   [非交互环境] 默认自动批准，继续流水线。")
            decision = {"approved": True, "note": "非交互环境自动批准", "approver": "system(auto)"}
        result = app.invoke(Command(resume=decision), config)

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="FacilityMind CLI")
    parser.add_argument("--id", help="工单 ID，如 T-001")
    parser.add_argument("--all", action="store_true", help="运行全部示例工单")
    parser.add_argument("--scenario", help="预设场景名 elevator_fault / hvac_fault / leak")
    parser.add_argument("--auto", action="store_true", help="跳过人工确认（自动批准）")
    parser.add_argument("--compare", action="store_true", help="对比规则库与 LLM 的结论差异（不跑完整流水线）")
    args = parser.parse_args()

    tickets = load_tickets()

    # 解析目标工单列表（id / scenario / all / 默认首条）
    if args.id:
        targets = [t for t in tickets if t["id"] == args.id]
    elif args.scenario:
        scenario_map = {"elevator_fault": "T-001", "hvac_fault": "T-003", "leak": "T-005"}
        tid = scenario_map.get(args.scenario)
        targets = [t for t in tickets if t["id"] == tid]
    elif args.all:
        targets = list(tickets)
    else:
        targets = [tickets[0]]

    if not targets:
        print("未找到目标工单（检查 --id / --scenario 参数）")
        return

    # 对比模式：只展示规则库 vs LLM 的结论差异
    if args.compare:
        from .compare import compare_ticket
        for t in targets:
            print(compare_ticket(t))
            print()
        return

    # 正常流水线模式
    for t in targets:
        config = {"configurable": {"thread_id": t["id"]}}
        _print_result(t["id"], run_one(t, config, auto_approve=args.auto or args.all))


if __name__ == "__main__":
    main()
