"""评估 harness：把多 Agent 工作流当成可度量系统来批量跑。

- 默认 auto_approve，批量跑全部工单不阻断；
- 每条工单采集：是否完成、是否需人工确认、QA 通过、SLA 达成、成本、token、步骤数；
- 聚合为完成率 / QA 通过率 / SLA 达成率 / 人工确认率 / 成本 / token 等指标；
- 终端直接看，或 --out 写 Markdown、--json 导原始指标。

离线规则模式下 token 恒为 0（未调大模型），报告标注运行模式，指标诚实可读。
"""

import argparse
import json

from .dataio import load_tickets
from .graph import app
from .knowledge import APPROVAL_THRESHOLD_COST
from .llm import llm, reset_all, total_tokens_all


def _count_steps(messages) -> int:
    """统计实际经过的 Agent 节点数（同一 Agent 只计一次）。"""
    tags = set()
    for m in messages:
        content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        if isinstance(content, str) and content.startswith("["):
            tags.add(content.split("]", 1)[0].strip("["))
    return len(tags)


def run_one(raw_ticket: dict) -> dict:
    """跑单条工单并抽取评估指标。"""
    reset_all()  # 清零计量，实现按工单拆分 token
    initial = {"ticket": raw_ticket, "auto_approve": True}
    result = app.invoke(initial, {"configurable": {"thread_id": "eval-" + raw_ticket["id"]}})

    diag = result.get("diagnosis", {})
    plan = result.get("assignment", {})
    approval = result.get("approval", {})
    feedback = result.get("feedback", {})
    qa = result.get("qa", {})
    report = result.get("report", {})

    completed = bool(report)
    sla_min = diag.get("sla_hours", 0) * 60
    sla_met = completed and feedback.get("actual_response_min", 0) <= sla_min

    return {
        "id": raw_ticket["id"],
        "type": result.get("ticket", {}).get("type", raw_ticket.get("type")),
        "urgency": result.get("ticket", {}).get("urgency"),
        "cost": plan.get("cost", 0.0),
        "approval_required": plan.get("cost", 0.0) > APPROVAL_THRESHOLD_COST,
        "completed": completed,
        "qa_passed": qa.get("passed", False),
        "qa_score": qa.get("score", 0.0),
        "sla_met": sla_met,
        "steps": _count_steps(result.get("messages", [])),
        "tokens": total_tokens_all(),
    }


def aggregate(records: list[dict]) -> dict:
    n = len(records) or 1
    total_cost = sum(r["cost"] for r in records)
    return {
        "total": len(records),
        "mode": "在线 LLM" if llm.available else "离线规则",
        "completion_rate": sum(1 for r in records if r["completed"]) / n,
        "qa_pass_rate": sum(1 for r in records if r["qa_passed"]) / n,
        "sla_rate": sum(1 for r in records if r["sla_met"]) / n,
        "approval_required_rate": sum(1 for r in records if r["approval_required"]) / n,
        "total_cost": total_cost,
        "avg_cost": total_cost / n,
        "total_tokens": sum(r["tokens"] for r in records),
        "avg_steps": sum(r["steps"] for r in records) / n,
    }


def render_report(records: list[dict], metrics: dict) -> str:
    lines = [
        "# FacilityMind 评估 Harness 报告",
        "",
        f"- 运行模式：**{metrics['mode']}**",
        f"- 工单总数：{metrics['total']}",
        f"- **任务完成率**：{metrics['completion_rate'] * 100:.1f}%",
        f"- **QA 通过率**：{metrics['qa_pass_rate'] * 100:.1f}%",
        f"- **SLA 达成率**：{metrics['sla_rate'] * 100:.1f}%",
        f"- 需人工确认比例：{metrics['approval_required_rate'] * 100:.1f}%",
        f"- 总处置成本：¥{metrics['total_cost']:.0f}（均值 ¥{metrics['avg_cost']:.0f}）",
        f"- Token 消耗：{metrics['total_tokens']}（{'在线' if metrics['mode'] == '在线 LLM' else '离线不计'}）",
        f"- 平均步骤数：{metrics['avg_steps']:.1f}",
        "",
        "## 逐工单明细",
        "",
        "| 工单 | 类型 | 紧急度 | 成本 | 需确认 | 完成 | QA | SLA | 步骤 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in records:
        lines.append(
            f"| {r['id']} | {r['type']} | {r['urgency']} | ¥{r['cost']:.0f} | "
            f"{'是' if r['approval_required'] else '否'} | "
            f"{'✓' if r['completed'] else '✗'} | "
            f"{'✓' if r['qa_passed'] else '✗'} | "
            f"{'✓' if r['sla_met'] else '✗'} | "
            f"{r['steps']} |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="FacilityMind 评估 harness")
    parser.add_argument("--all", action="store_true", help="评估全部内置工单")
    parser.add_argument("--id", action="append", help="只评估指定工单 ID（可重复）")
    parser.add_argument("--out", help="把 Markdown 报告写入该路径")
    parser.add_argument("--json", help="把原始指标导出为 JSON 到该路径")
    args = parser.parse_args()

    tickets = load_tickets()
    if args.id:
        ids = set(args.id)
        selected = [t for t in tickets if t["id"] in ids]
        if not selected:
            print(f"未找到工单 {args.id}")
            return
    else:
        selected = tickets

    records = [run_one(t) for t in selected]
    metrics = aggregate(records)
    report = render_report(records, metrics)
    print(report)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\n报告已写入 {args.out}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"metrics": metrics, "records": records}, f, ensure_ascii=False, indent=2)
        print(f"JSON 已写入 {args.json}")


if __name__ == "__main__":
    main()
