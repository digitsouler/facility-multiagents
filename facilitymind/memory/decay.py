"""记忆衰减、沉淀与归档（Phase 6.3）。

设计目标：让记忆"活着但不过期污染"——
- recency()     时间衰减权重：越久的事件记忆，在检索时权重越低（τ≈180 天）。
- consolidate() 沉淀：把"经 QA 校验有效"的处置固化进 kb_learnings（长期知识），
                  不随时间衰减，作为跨楼宇可复用的经验底座。
- archive_old() 归档：超过保留期(默认 365 天)的事件记忆转入 incidents_archive，
                  保持活跃检索集聚焦近期，且保留可审计。
- run_maintenance() 一次性执行沉淀+归档，返回摘要；供 Web / CLI 调用。

所有函数在 store.disabled（记忆不可用）时安全降级为 no-op，绝不拖垮主流水线。
"""

from datetime import datetime, timedelta
from math import exp

from .store import get_store

# 时间衰减时间常数（天）：约 180 天后记忆权重降到 ~37%
TAU_DAYS = 180.0
# 事件记忆保留期（天）：超过则转入归档表
RETENTION_DAYS = 365


def recency(created_at: str, tau_days: float = TAU_DAYS) -> float:
    """计算时间衰减权重 ∈ (0, 1]，今日=1.0。解析失败按最新处理。"""
    try:
        age = (datetime.now() - datetime.fromisoformat(created_at)).days
    except Exception:
        return 1.0
    if age < 0:
        age = 0
    return float(exp(-age / tau_days))


def consolidate(store=None, min_support: int = 1) -> dict:
    """把未沉淀且 QA 有效的事件记忆，按 (类型, 资产类型, 根因) 分组沉淀为长期知识。

    返回摘要：{promoted, kb_added, kb_updated, groups}。store 不可用时返回 disabled。
    """
    store = store or get_store()
    if store.disabled:
        return {"disabled": True, "promoted": 0, "kb_added": 0, "kb_updated": 0}
    rows = store.get_unpromoted_validated()
    if not rows:
        return {"promoted": 0, "kb_added": 0, "kb_updated": 0, "groups": 0}

    # 分组：同一 (类型, 资产类型, 根因) 视为同一经验模式
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (r["ticket_type"], r["asset_type"], r["root_cause"])
        groups.setdefault(key, []).append(r)

    kb_added = kb_updated = 0
    promoted_ids: list[int] = []
    for (tt, at, rc), grp in groups.items():
        if len(grp) < min_support:
            continue
        # 已有沉淀知识判定新增/更新（用于摘要统计）
        existing = {k["root_cause"] for k in store.get_kb_learnings(ticket_type=tt, asset_type=at)}
        weight = float(len(grp))  # 出现次数即权重，出现越多越可信
        was_new = rc not in existing
        store.upsert_kb(
            ticket_type=tt, asset_type=at, root_cause=rc,
            recommended_action=grp[0]["recommended_action"], delta_weight=weight,
        )
        kb_added += 1 if was_new else 0
        kb_updated += 0 if was_new else 1
        promoted_ids.extend(r["id"] for r in grp)

    store.mark_promoted(promoted_ids)
    return {
        "promoted": len(promoted_ids),
        "kb_added": kb_added,
        "kb_updated": kb_updated,
        "groups": len(groups),
    }


def archive_old(store=None, retention_days: int = RETENTION_DAYS) -> dict:
    """把早于保留期的事件记忆转入归档表；返回 {archived, cutoff}。"""
    store = store or get_store()
    if store.disabled:
        return {"disabled": True, "archived": 0}
    cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat(timespec="seconds")
    ids = store.select_for_archive(cutoff)
    n = store.archive_incidents(ids) if ids else 0
    return {"archived": n, "cutoff": cutoff}


def run_maintenance(store=None, retention_days: int = RETENTION_DAYS, min_support: int = 1) -> dict:
    """一次性执行沉淀+归档，并返回当前统计快照。供 Web / CLI 调用。"""
    store = store or get_store()
    cons = consolidate(store=store, min_support=min_support)
    arch = archive_old(store=store, retention_days=retention_days)
    try:
        stats = store.stats()
    except Exception:
        stats = {}
    return {"consolidate": cons, "archive": arch, "stats": stats}
