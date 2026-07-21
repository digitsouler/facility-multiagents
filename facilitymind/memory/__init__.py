"""记忆层：可持久化、可离线回退的多 Agent 经验记忆。

对外暴露单例 get_store()，Agent 侧只调用 add_incident / add_asset_knowledge /
add_kb_learning / get_incidents / get_asset_knowledge 等高层接口即可。
decay 提供时间衰减、沉淀（consolidate）与归档（archive_old）的维护能力。
所有方法在数据库不可用时自动降级为 no-op，绝不拖垮主流水线。
"""

from .store import MemoryStore, get_store
from .decay import recency, consolidate, archive_old, run_maintenance, TAU_DAYS, RETENTION_DAYS

__all__ = [
    "MemoryStore", "get_store",
    "recency", "consolidate", "archive_old", "run_maintenance",
    "TAU_DAYS", "RETENTION_DAYS",
]
