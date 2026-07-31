"""记忆层：维保商口碑(Redis) + 案例库(Qdrant)。

两层存储各管一层，与确定性知识库(KB)不重叠：
- redis_vendor：维保商运营绩效（高频原子累加，排名用）。
- qdrant_cases：历史案例语义检索（向量）。坏案例也入库(payload 带 recommendations)，不单独写本地 KB。
"""

from .redis_vendor import seed as seed_vendors, vendor_quality, update as update_vendor
from .qdrant_cases import save as qdrant_save, recall_cases, verify_new_issue

__all__ = [
    "seed_vendors",
    "vendor_quality",
    "update_vendor",
    "qdrant_save",
    "recall_cases",
    "verify_new_issue",
]
