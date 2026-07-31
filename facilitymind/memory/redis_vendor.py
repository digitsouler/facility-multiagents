"""维保商口碑存储（Redis）。

首次从 knowledge.VENDORS 冷 seed；之后只在结案/QA 阶段回写 jobs_*/cost_sum。
Redis 不可用（未起/认证失败/协议不兼容）时回退进程内内存 dict，保证主流程不中断。
"""

import logging
import os
import time

from ..knowledge import VENDORS

log = logging.getLogger("facilitymind.memory.redis")

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_DB = int(os.environ.get("REDIS_DB", "2"))
KEY_PREFIX = "facility:vendor:"

_client = None
_status = None  # None=未探测, True/False
_mem: dict = {}


def _get_client():
    global _client, _status
    if _client is not None or _status is False:
        return _client
    try:
        import redis

        r = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=REDIS_DB,
            socket_connect_timeout=2,
            socket_timeout=2,
            protocol=2, 
        )
        r.ping()
        _client = r
        _status = True
        log.info("[redis] 连接成功 db=%d", REDIS_DB)
    except Exception as e:  # noqa: BLE001
        _status = False
        log.warning("[redis] 不可用，口碑回退内存模式：%s", repr(e)[:120])
    return _client


def seed() -> None:
    """首次把 VENDORS 写进 Redis（已存在跳过）。"""
    r = _get_client()
    if r is None:
        return
    for v in VENDORS:
        key = KEY_PREFIX + v["name"]
        if r.exists(key):
            continue
        r.hset(key, "skill", v["skill"])
        r.hset(key, "base_cost", v["cost"])
        r.hset(key, "base_resp_min", v["response_min"])
        r.hset(key, "jobs_total", 0)
        r.hset(key, "jobs_passed", 0)
        r.hset(key, "cost_sum", 0.0)
        r.hset(key, "rework", 0)
        r.hset(key, "updated_at", "")
    log.info("[redis] seed 完成，维保商数=%d", len(VENDORS))


def update(ticket_id: str, vendor_name: str, cost: float, qa_passed: bool) -> None:
    """结案后回写一次处置结果（原子累加）。"""
    r = _get_client()
    key = KEY_PREFIX + vendor_name
    if r is None:
        _mem_update(vendor_name, cost, qa_passed)
        return
    r.hincrby(key, "jobs_total", 1)
    if qa_passed:
        r.hincrby(key, "jobs_passed", 1)
    else:
        r.hincrby(key, "rework", 1)
    r.hincrbyfloat(key, "cost_sum", float(cost))
    r.hset(key, "updated_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    log.info("[redis] update %s qa=%s cost=%.0f", vendor_name, qa_passed, cost)


def vendor_quality(vendor_name: str) -> float:
    """口碑质量分 = 通过/总数；无历史回退 0.9 冷启动。"""
    r = _get_client()
    data = None
    if r is not None:
        raw = r.hgetall(KEY_PREFIX + vendor_name)
        if raw:
            data = {
                (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                for k, v in raw.items()
            }
    else:
        data = _mem.get(vendor_name)
    if not data:
        return 0.9
    total = int(data.get("jobs_total", 0))
    if total == 0:
        return 0.9
    return round(int(data.get("jobs_passed", 0)) / total, 3)


def _mem_update(name, cost, qa_passed) -> None:
    d = _mem.setdefault(name, {"jobs_total": 0, "jobs_passed": 0, "cost_sum": 0.0})
    d["jobs_total"] += 1
    if qa_passed:
        d["jobs_passed"] += 1
    d["cost_sum"] += cost
