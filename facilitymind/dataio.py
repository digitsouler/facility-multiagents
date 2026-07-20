"""数据读写：合成工单/资源池的加载入口。"""

import json
import os

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def load_tickets() -> list[dict]:
    path = os.path.join(DATA_DIR, "tickets.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_vendors() -> list[dict]:
    # 当前 vendor 资源池来自 knowledge.VENDORS，这里保留接口以便后续接入 CMMS。
    from .knowledge import VENDORS

    return VENDORS
