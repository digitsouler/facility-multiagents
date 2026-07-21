"""SQLite 支撑的记忆存储。

三张表：
- incidents      事件记忆（episodic）：每单工单的处置结果，带 building/asset_type 标签
- asset_knowledge 资产·知识记忆（semantic）：每台设备的维修档案（优选供应商/均成本/均耗时）
- kb_learnings   沉淀知识（semantic）：经 QA 校验有效的根因→处置，作为长期不衰减知识

设计原则（与 LLM/MCP 回退一致）：数据库不可用 → 全部方法降级为 no-op，
主流水线照常运行，绝不抛异常中断。
"""

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = REPO_ROOT / "data" / "facility_memory.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id      TEXT,
    ticket_type    TEXT,
    building       TEXT,
    asset_id       TEXT,
    asset_type     TEXT,
    urgency        TEXT,
    root_cause     TEXT,
    recommended_action TEXT,
    required_skill TEXT,
    vendor         TEXT,
    cost           REAL,
    estimated_cost REAL,
    actual_response_min REAL,
    sla_hours      INTEGER,
    confidence     REAL,
    recurrence     INTEGER,
    evidence       TEXT,
    outcome_valid  INTEGER,
    promoted       INTEGER DEFAULT 0,
    created_at     TEXT
);
CREATE TABLE IF NOT EXISTS incidents_archive (
    archive_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    id             INTEGER,
    ticket_id      TEXT,
    ticket_type    TEXT,
    building       TEXT,
    asset_id       TEXT,
    asset_type     TEXT,
    urgency        TEXT,
    root_cause     TEXT,
    recommended_action TEXT,
    required_skill TEXT,
    vendor         TEXT,
    cost           REAL,
    estimated_cost REAL,
    actual_response_min REAL,
    sla_hours      INTEGER,
    confidence     REAL,
    recurrence     INTEGER,
    evidence       TEXT,
    outcome_valid  INTEGER,
    promoted       INTEGER DEFAULT 0,
    created_at     TEXT,
    archived_at    TEXT
);
CREATE TABLE IF NOT EXISTS asset_knowledge (
    asset_id       TEXT PRIMARY KEY,
    building       TEXT,
    asset_type     TEXT,
    preferred_vendor TEXT,
    avg_cost       REAL,
    avg_response_min REAL,
    sample_n       INTEGER DEFAULT 1,
    updated_at     TEXT
);
CREATE TABLE IF NOT EXISTS kb_learnings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_type    TEXT,
    asset_type     TEXT,
    root_cause     TEXT,
    recommended_action TEXT,
    weight         REAL DEFAULT 1.0,
    source_incident_id TEXT,
    created_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_type ON incidents(ticket_type);
CREATE INDEX IF NOT EXISTS idx_incidents_asset ON incidents(asset_id);
CREATE INDEX IF NOT EXISTS idx_incidents_building ON incidents(building);
"""


class MemoryStore:
    """SQLite 记忆存储，自带离线回退。"""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.disabled = False
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._migrate()  # 老库补列（promoted），新建表（incidents_archive）
        except Exception as exc:  # 任何初始化失败 → 降级
            self.disabled = True
            self._conn = None
            self._error = f"{type(exc).__name__}: {exc}"

    def _migrate(self) -> None:
        """兼容已存在的库：补 promoted 列；新建 incidents_archive 表（CREATE 已含）。"""
        if self.disabled or self._conn is None:
            return
        try:
            cols = [r["name"] for r in self._conn.execute("PRAGMA table_info(incidents)").fetchall()]
            if "promoted" not in cols:
                self._conn.execute("ALTER TABLE incidents ADD COLUMN promoted INTEGER DEFAULT 0")
                self._conn.commit()
        except Exception:
            pass

    # ---- 写入：事件记忆 ----
    def add_incident(self, *, ticket_id, ticket_type, building, asset_id, asset_type,
                     urgency, root_cause, recommended_action, required_skill,
                     vendor, cost, estimated_cost, actual_response_min, sla_hours,
                     confidence, recurrence, evidence, outcome_valid) -> Optional[int]:
        if self.disabled:
            return None
        try:
            cur = self._conn.execute(
                """INSERT INTO incidents
                   (ticket_id, ticket_type, building, asset_id, asset_type, urgency,
                    root_cause, recommended_action, required_skill, vendor, cost,
                    estimated_cost, actual_response_min, sla_hours, confidence,
                    recurrence, evidence, outcome_valid, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ticket_id, ticket_type, building, asset_id, asset_type, urgency,
                 root_cause, recommended_action, required_skill, vendor, cost,
                 estimated_cost, actual_response_min, sla_hours, confidence,
                 1 if recurrence else 0, evidence, 1 if outcome_valid else 0,
                 datetime.now().isoformat(timespec="seconds")),
            )
            self._conn.commit()
            return cur.lastrowid
        except Exception:
            return None

    # ---- 写入：资产·知识记忆（增量更新）----
    def add_asset_knowledge(self, *, asset_id, building, asset_type, vendor,
                            cost, actual_response_min) -> None:
        if self.disabled or not asset_id:
            return
        try:
            row = self._conn.execute(
                "SELECT avg_cost, avg_response_min, sample_n FROM asset_knowledge WHERE asset_id=?",
                (asset_id,),
            ).fetchone()
            now = datetime.now().isoformat(timespec="seconds")
            if row is None:
                self._conn.execute(
                    """INSERT INTO asset_knowledge
                       (asset_id, building, asset_type, preferred_vendor,
                        avg_cost, avg_response_min, sample_n, updated_at)
                       VALUES (?,?,?,?,?,?,1,?)""",
                    (asset_id, building, asset_type, vendor, cost, actual_response_min, now),
                )
            else:
                n = row["sample_n"] + 1
                avg_cost = (row["avg_cost"] * row["sample_n"] + cost) / n
                avg_resp = (row["avg_response_min"] * row["sample_n"] + actual_response_min) / n
                self._conn.execute(
                    """UPDATE asset_knowledge SET preferred_vendor=?, avg_cost=?,
                       avg_response_min=?, sample_n=?, updated_at=? WHERE asset_id=?""",
                    (vendor, avg_cost, avg_resp, n, now, asset_id),
                )
            self._conn.commit()
        except Exception:
            return None

    # ---- 写入：沉淀知识（长期，不衰减）----
    def add_kb_learning(self, *, ticket_type, asset_type, root_cause,
                        recommended_action, weight=1.0, source_incident_id=None) -> Optional[int]:
        if self.disabled:
            return None
        try:
            cur = self._conn.execute(
                """INSERT INTO kb_learnings
                   (ticket_type, asset_type, root_cause, recommended_action,
                    weight, source_incident_id, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (ticket_type, asset_type, root_cause, recommended_action, weight,
                 source_incident_id, datetime.now().isoformat(timespec="seconds")),
            )
            self._conn.commit()
            return cur.lastrowid
        except Exception:
            return None

    # ---- 读取：事件记忆（按标签过滤）----
    def get_incidents(self, *, ticket_type=None, building=None, asset_id=None,
                      asset_type=None, limit=20) -> list[dict]:
        if self.disabled:
            return []
        try:
            clauses, params = [], []
            if ticket_type:
                clauses.append("ticket_type=?")
                params.append(ticket_type)
            if building:
                clauses.append("building=?")
                params.append(building)
            if asset_id:
                clauses.append("asset_id=?")
                params.append(asset_id)
            if asset_type:
                clauses.append("asset_type=?")
                params.append(asset_type)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = self._conn.execute(
                f"SELECT * FROM incidents{where} ORDER BY id DESC LIMIT ?",
                params + [limit],
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_asset_knowledge(self, asset_id: str) -> Optional[dict]:
        if self.disabled or not asset_id:
            return None
        try:
            row = self._conn.execute(
                "SELECT * FROM asset_knowledge WHERE asset_id=?", (asset_id,)
            ).fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    def get_kb_learnings(self, *, ticket_type=None, asset_type=None, limit=10) -> list[dict]:
        if self.disabled:
            return []
        try:
            clauses, params = [], []
            if ticket_type:
                clauses.append("ticket_type=?")
                params.append(ticket_type)
            if asset_type:
                clauses.append("asset_type=?")
                params.append(asset_type)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = self._conn.execute(
                f"SELECT * FROM kb_learnings{where} ORDER BY weight DESC, id DESC LIMIT ?",
                params + [limit],
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    # ---- 写入（沉淀/归档专用，供 decay.py 调用）----
    def get_unpromoted_validated(self, limit: int = 500) -> list[dict]:
        """取出"经 QA 校验有效且尚未沉淀"的事件记忆（供 consolidate 沉淀为长期知识）。"""
        if self.disabled:
            return []
        try:
            rows = self._conn.execute(
                "SELECT * FROM incidents WHERE outcome_valid=1 AND promoted=0 "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def mark_promoted(self, ids: list[int]) -> None:
        """标记事件记忆已沉淀（避免重复沉淀）。"""
        if self.disabled or not ids:
            return
        try:
            q = "UPDATE incidents SET promoted=1 WHERE id IN ({})".format(
                ",".join("?" * len(ids))
            )
            self._conn.execute(q, tuple(ids))
            self._conn.commit()
        except Exception:
            pass

    def upsert_kb(self, *, ticket_type, asset_type, root_cause, recommended_action,
                  delta_weight: float = 1.0) -> Optional[int]:
        """沉淀知识 upsert：同 (type, asset_type, root_cause) 累加权重，否则新建。"""
        if self.disabled:
            return None
        try:
            row = self._conn.execute(
                "SELECT id, weight FROM kb_learnings "
                "WHERE ticket_type=? AND asset_type=? AND root_cause=?",
                (ticket_type, asset_type, root_cause),
            ).fetchone()
            if row:
                new_w = row["weight"] + delta_weight
                self._conn.execute(
                    "UPDATE kb_learnings SET weight=?, recommended_action=? WHERE id=?",
                    (new_w, recommended_action, row["id"]),
                )
                self._conn.commit()
                return row["id"]
            cur = self._conn.execute(
                """INSERT INTO kb_learnings
                   (ticket_type, asset_type, root_cause, recommended_action, weight, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (ticket_type, asset_type, root_cause, recommended_action, delta_weight,
                 datetime.now().isoformat(timespec="seconds")),
            )
            self._conn.commit()
            return cur.lastrowid
        except Exception:
            return None

    def select_for_archive(self, cutoff_iso: str, limit: int = 1000) -> list[int]:
        """取出早于 cutoff 的事件记忆 id（供归档）。"""
        if self.disabled:
            return []
        try:
            rows = self._conn.execute(
                "SELECT id FROM incidents WHERE created_at < ? ORDER BY id ASC LIMIT ?",
                (cutoff_iso, limit),
            ).fetchall()
            return [r["id"] for r in rows]
        except Exception:
            return []

    def archive_incidents(self, ids: list[int]) -> int:
        """把给定事件记忆复制到归档表并从活跃表删除；返回实际归档条数。"""
        if self.disabled or not ids:
            return 0
        try:
            n = 0
            arch = datetime.now().isoformat(timespec="seconds")
            for iid in ids:
                row = self._conn.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
                if not row:
                    continue
                cols = [c for c in row.keys()]
                placeholders = ",".join("?" * len(cols))
                self._conn.execute(
                    f"INSERT OR IGNORE INTO incidents_archive ({','.join(cols)}, archived_at) "
                    f"VALUES ({placeholders}, ?)",
                    tuple(row) + (arch,),
                )
                self._conn.execute("DELETE FROM incidents WHERE id=?", (iid,))
                n += 1
            self._conn.commit()
            return n
        except Exception:
            return 0

    # ---- 统计 ----
    def stats(self) -> dict:
        if self.disabled:
            return {"disabled": True, "error": getattr(self, "_error", "unknown")}
        try:
            n_inc = self._conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
            n_ast = self._conn.execute("SELECT COUNT(*) FROM asset_knowledge").fetchone()[0]
            n_kb = self._conn.execute("SELECT COUNT(*) FROM kb_learnings").fetchone()[0]
            n_arch = self._conn.execute("SELECT COUNT(*) FROM incidents_archive").fetchone()[0]
            return {"disabled": False, "incidents": n_inc,
                    "asset_knowledge": n_ast, "kb_learnings": n_kb, "archived": n_arch}
        except Exception:
            return {"disabled": True, "error": "stats failed"}

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass


_store: Optional[MemoryStore] = None


def get_store() -> MemoryStore:
    """懒加载单例。"""
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store
