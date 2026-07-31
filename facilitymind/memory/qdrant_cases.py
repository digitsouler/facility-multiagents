"""案例库存储（Qdrant 向量 + 本地 JSONL 双写）。

在线：embedding 后 upsert 进 Qdrant，召回走向量相似（过滤好案例）。
离线 / embed 失败：回退 cases.jsonl 关键词检索。
坏案例也入库（outcome=bad，payload 带 recommendations）
"""

import json
import logging
import os

log = logging.getLogger("facilitymind.memory.qdrant")

COLLECTION = "facility_cases"
VECTOR_DIM = 2048

_client = None
_status = None


def _embed(text: str):
    from dotenv import load_dotenv

    load_dotenv()
    import os as _os
    from openai import OpenAI

    k = _os.environ.get("ZHIPU_API_KEY")
    if not k:
        return None
    try:
        c = OpenAI(api_key=k, base_url="https://open.bigmodel.cn/api/paas/v4", timeout=20)
        r = c.embeddings.create(model="embedding-3", input=text)
        return r.data[0].embedding
    except Exception as e:  # noqa: BLE001
        log.warning("[qdrant] embed 失败，回退关键词：%s", repr(e)[:120])
        return None


def _get_client():
    global _client, _status
    if _client is not None or _status is False:
        return _client
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams

        c = QdrantClient(host="localhost", port=6333, timeout=3)
        if not c.collection_exists(COLLECTION):
            c.create_collection(
                COLLECTION, vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE)
            )
        _client = c
        _status = True
        log.info("[qdrant] 连接成功，集合=%s", COLLECTION)
    except Exception as e:  # noqa: BLE001
        _status = False
        log.warning("[qdrant] 不可用，案例召回回退 JSONL：%s", repr(e)[:120])
    return _client


def _point_id(cid: str) -> int:
    return abs(hash(cid)) % (2**63)


def save(case: dict) -> dict:
    """写案例：先 JSONL再 Qdrant。返回 {saved, case_id}。"""
    from ..services import save_case as _json_save

    res = _json_save(case)  # JSONL 幂等
    c = _get_client()
    if c is None:
        return res
    cid = case.get("case_id") or f"CASE-{case.get('ticket_id')}"
    text = " ".join(str(case.get(k, "")) for k in ("type", "location", "root_cause", "recommended_action"))
    vec = _embed(text)
    if vec is None:
        return res
    from qdrant_client.models import PointStruct

    keys = (
        "ticket_id",
        "type",
        "location",
        "root_cause",
        "recommended_action",
        "vendor",
        "cost",
        "qa_score",
        "qa_passed",
        "outcome",
        "recurrence",
        "recommendations",
        "created_at",
    )
    payload = {k: case[k] for k in keys if k in case}
    c.upsert(COLLECTION, [PointStruct(id=_point_id(cid), vector=vec, payload=payload)])
    log.info("[qdrant] save %s", cid)
    return res


def recall_cases(fault_type: str, location: str = "", raw: str = "", top_k: int = 3, good_only: bool = True):
    """召回相似好案例。在线查向量；离线/无向量回退 JSONL 关键词。"""
    c = _get_client()
    vec = None
    if c is not None:
        text = " ".join(x for x in (fault_type, location, raw) if x)
        vec = _embed(text)
    if c is not None and vec is not None:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        f = Filter(must=[FieldCondition(key="qa_passed", match=MatchValue(value=True))]) if good_only else None
        try: 
            resp = c.query_points(collection_name=COLLECTION, query=vec, limit=top_k, query_filter=f)
            hits = resp.points
        except AttributeError:
            hits = c.search(COLLECTION, vec, limit=top_k, query_filter=f)
        out = [h.payload for h in hits if h.payload]
        log.info("[qdrant] recall(%s) 向量命中 %d", fault_type, len(out))
        return out
    return _recall_jsonl(fault_type, location, top_k, good_only)


def _recall_jsonl(fault_type, location, top_k, good_only):
    from ..services import CASE_PATH

    if not os.path.exists(CASE_PATH):
        return []
    out = []
    try:
        with open(CASE_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                c = json.loads(line)
                if good_only and not c.get("qa_passed"):
                    continue
                if fault_type and c.get("type") != fault_type:
                    continue
                if location and c.get("location") and location not in c.get("location", ""):
                    continue
                out.append(c)
    except (OSError, json.JSONDecodeError):
        return []
    log.info("[qdrant] recall(回退JSONL %s) 命中 %d", fault_type, len(out[:top_k]))
    return out[:top_k]


def verify_new_issue(raw: str, fault_type: str, skill: str) -> dict:
    """新问题查证：并行查历史案例 + 维保商技能，给出候选；全无则标记需人工。
    """
    from .redis_vendor import _get_client as _redis, KEY_PREFIX

    cases = recall_cases(fault_type, raw=raw, top_k=3, good_only=False)
    vendors = []
    r = _redis()
    if r is not None:
        for name in r.keys(KEY_PREFIX + "*"):
            name = name.decode() if isinstance(name, bytes) else name
            v = r.hgetall(name)
            v = {
                (k.decode() if isinstance(k, bytes) else k): (val.decode() if isinstance(val, bytes) else val)
                for k, val in v.items()
            }
            if v.get("skill") == skill:
                vendors.append(name[len(KEY_PREFIX) :])
    else:
        from ..knowledge import VENDORS

        vendors = [v["name"] for v in VENDORS if v["skill"] == skill]
    needs_human = len(cases) == 0 and len(vendors) == 0
    log.info(
        "[Verify] 新问题(%s) 案例=%d 候选商=%d → %s",
        fault_type,
        len(cases),
        len(vendors),
        "需人工" if needs_human else "有候选",
    )
    return {
        "is_new": True,
        "similar_cases": cases,
        "candidate_vendors": vendors,
        "needs_human": needs_human,
    }
