"""评估 harness：把多 Agent 工作流当成可度量系统来跑。

目标：一键得到"这套多 Agent 方案到底好不好用"的量化证据，而不是只看单条 demo。
- 批量跑内置工单（默认 auto_approve，避免人工确认阻断批量评估）；
- 每条工单采集：是否完成、是否需人工确认、QA 通过、SLA 达成、成本、token、步骤数；
- 聚合为完成率 / QA 通过率 / SLA 达成率 / 人工确认率 / 成本 / token 等指标；
- 一键渲染 Markdown 报告（--out）或导出 JSON（--json），也可终端直接看。

离线规则模式下 token 恒为 0（未调用大模型），报告会标注运行模式，保证指标诚实可读。
接真实大模型后，token 字段即真实用量，可直接用于成本对比（例如规则 vs LLM 的成本/质量权衡）。
"""

import argparse
import json

from .dataio import load_tickets
from .graph import app
from .knowledge import APPROVAL_THRESHOLD_COST
from .llm import llm


def _content_of(msg) -> str:
    """兼容 langchain 消息对象与纯 dict 两种形态。"""
    if isinstance(msg, dict):
        c = msg.get("content", "")
        return c if isinstance(c, str) else ""
    return getattr(msg, "content", "") or ""


def _count_steps(messages) -> int:
    """统计实际经过的 Agent 节点数（同一 Agent 只计一次）。"""
    tags = set()
    for m in messages:
        content = _content_of(m)
        if isinstance(content, str) and content.startswith("["):
            tag = content.split("]", 1)[0].strip("[")
            tags.add(tag)
    return len(tags)


def run_one(raw_ticket: dict) -> dict:
    """跑单条工单并抽取评估指标。"""
    llm.reset()
    initial = {"ticket": raw_ticket, "auto_approve": True}
    # 用 eval- 前缀 thread_id，避免与 Dashboard 看板的流式 thread_id 互相污染 MemorySaver 状态
    result = app.invoke(initial, {"configurable": {"thread_id": "eval-" + raw_ticket["id"]}})

    diag = result.get("diagnosis", {})
    plan = result.get("dispatch_plan", {})
    approval = result.get("approval", {})
    execution = result.get("execution", {})
    qa = result.get("qa", {})
    report = result.get("report", {})

    completed = bool(report)
    sla_min = diag.get("sla_hours", 0) * 60
    sla_met = completed and execution.get("actual_response_min", 0) <= sla_min
    cost = plan.get("cost", 0.0)

    return {
        "id": raw_ticket["id"],
        "type": result.get("ticket", {}).get("type", raw_ticket.get("type")),
        "urgency": result.get("ticket", {}).get("urgency"),
        "cost": cost,
        "estimated_cost": diag.get("estimated_cost", 0.0),
        "approval_required": cost > APPROVAL_THRESHOLD_COST,
        "approved": approval.get("approved", completed),
        "completed": completed,
        "qa_passed": qa.get("passed", False),
        "qa_score": qa.get("score", 0.0),
        "sla_met": sla_met,
        "actual_response_min": execution.get("actual_response_min", 0),
        "tokens": llm.total_tokens,
        "llm_calls": llm.call_count,
        "steps": _count_steps(result.get("messages", [])),
        "recurrence": diag.get("recurrence", False),
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
        "approval_rate": sum(1 for r in records if r["approved"]) / n,
        "total_cost": total_cost,
        "avg_cost": total_cost / n,
        "total_tokens": sum(r["tokens"] for r in records),
        "total_llm_calls": sum(r["llm_calls"] for r in records),
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
        f"- 人工确认通过率：{metrics['approval_rate'] * 100:.1f}%",
        f"- 总处置成本：¥{metrics['total_cost']:.0f}（均值 ¥{metrics['avg_cost']:.0f}）",
        f"- Token 消耗：{metrics['total_tokens']}（LLM 调用 {metrics['total_llm_calls']} 次）",
        f"- 平均步骤数：{metrics['avg_steps']:.1f}",
        "",
        "## 逐工单明细",
        "",
        "| 工单 | 类型 | 紧急度 | 成本 | 需确认 | 完成 | QA | SLA | 步骤 | Token |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in records:
        lines.append(
            f"| {r['id']} | {r['type']} | {r['urgency']} | ¥{r['cost']:.0f} | "
            f"{'是' if r['approval_required'] else '否'} | "
            f"{'✓' if r['completed'] else '✗'} | "
            f"{'✓' if r['qa_passed'] else '✗'} | "
            f"{'✓' if r['sla_met'] else '✗'} | "
            f"{r['steps']} | {r['tokens']} |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="FacilityMind 评估 harness")
    parser.add_argument("--all", action="store_true", help="评估全部内置工单")
    parser.add_argument("--id", help="只评估指定工单 ID")
    parser.add_argument("--out", help="把 Markdown 报告写入该路径")
    parser.add_argument("--json", help="把原始指标导出为 JSON 到该路径")
    parser.add_argument("--quiet", action="store_true", help="只打印汇总指标")
    args = parser.parse_args()

    tickets = load_tickets()
    if args.id:
        selected = [t for t in tickets if t["id"] == args.id]
        if not selected:
            print(f"未找到工单 {args.id}")
            return
    else:
        selected = tickets

    records = [run_one(t) for t in selected]
    metrics = aggregate(records)
    report = render_report(records, metrics)

    if args.quiet:
        # 仅打印关键指标行（去掉明细表）
        summary = "\n".join(report.splitlines()[:11])
        print(summary)
    else:
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
