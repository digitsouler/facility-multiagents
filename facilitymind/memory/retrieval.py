"""记忆检索：把持久化记忆变成 Agent 可消费的上下文。

分层检索策略（跨楼宇边界，见规划）：
- 同楼宇 + 同资产        权重最高（本楼该设备自己的历史）
- 同资产（跨楼宇）       权重次高（经验复用）
- 同故障类型（跨楼宇）   权重兜底（通用规律）
再叠乘时间衰减 recency=exp(-age/τ)，并对"QA 有效"的处置加权。

零向量库依赖、完全离线、可解释——返回的是带分数的原始记录，便于前端/日志展示。
"""

from datetime import datetime
from math import exp

from ..mcp import providers
from .store import get_store

TAU_DAYS = 180.0


def _asset_of(ticket: dict) -> str:
    try:
        return providers._resolve_asset(ticket) or ""
    except Exception:
        return ""


def retrieve_similar(ticket: dict, limit: int = 5, tau_days: float = TAU_DAYS) -> list[dict]:
    """返回按相关度排序的历史事件记忆（排除当前工单自身）。"""
    store = get_store()
    if store.disabled:
        return []
    asset_id = _asset_of(ticket)
    building = ticket.get("location_hint") or ""
    rows = store.get_incidents(ticket_type=ticket["type"], limit=200)
    now = datetime.now()
    scored = []
    for r in rows:
        if r.get("ticket_id") == ticket.get("id"):
            continue
        same_asset = bool(asset_id) and r.get("asset_id") == asset_id
        same_building = bool(building) and r.get("building") == building
        if same_asset and same_building:
            base = 3.0
        elif same_asset:
            base = 2.0
        elif r.get("asset_type") == ticket["type"]:
            base = 1.0
        else:
            base = 0.3
        try:
            age_days = (now - datetime.fromisoformat(r["created_at"])).days
        except Exception:
            age_days = 0
        recency = exp(-age_days / tau_days)
        score = base * recency
        if r.get("outcome_valid") == 1:
            score *= 1.2
        elif r.get("outcome_valid") == 0:
            score *= 0.6
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]


def get_asset_context(ticket: dict) -> dict | None:
    """返回该资产的维修档案（semantic 记忆），无则 None。"""
    store = get_store()
    if store.disabled:
        return None
    asset_id = _asset_of(ticket)
    return store.get_asset_knowledge(asset_id) if asset_id else None


def get_kb_context(ticket: dict, limit: int = 5) -> list[dict]:
    """返回已沉淀的长期知识（经 QA 校验、不衰减），供 Diagnose 直接参考。"""
    store = get_store()
    if store.disabled:
        return []
    return store.get_kb_learnings(ticket_type=ticket["type"], asset_type=ticket["type"], limit=limit)


def format_memory_context(similar: list[dict], asset_ctx: dict | None, kb: list[dict] | None = None) -> str:
    """把检索结果拼成可注入 LLM 提示词的文本。无记忆返回空串。"""
    if not similar and not asset_ctx and not kb:
        return ""
    parts = []
    if asset_ctx:
        parts.append(
            f"资产档案（{asset_ctx['asset_id']}）：优选供应商 {asset_ctx['preferred_vendor']}，"
            f"平均成本 ¥{asset_ctx['avg_cost']:.0f}，平均响应 {asset_ctx['avg_response_min']:.0f} 分钟"
            f"（样本 {asset_ctx['sample_n']}）。"
        )
    if similar:
        lines = []
        for r in similar[:3]:
            tag = "有效" if r.get("outcome_valid") == 1 else ("无效" if r.get("outcome_valid") == 0 else "未知")
            loc = r.get("asset_id") or r.get("asset_type")
            lines.append(
                f"- [{r.get('building')}/{loc}] 根因：{r.get('root_cause')}；"
                f"处置：{r.get('recommended_action')}；质检{tag}"
            )
        parts.append("相似历史工单（按相关度）：\n" + "\n".join(lines))
    if kb:
        lines = []
        for k in kb[:3]:
            lines.append(
                f"- 根因：{k.get('root_cause')}；处置：{k.get('recommended_action')}（权重 {float(k.get('weight', 1)):.1f}）"
            )
        parts.append("沉淀知识（经 QA 校验，长期有效、跨楼宇可复用）：\n" + "\n".join(lines))
    return "\n\n参考历史经验：\n" + "\n".join(parts)
