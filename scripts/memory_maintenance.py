#!/usr/bin/env python
"""记忆层维护脚本（Phase 6.5）。

一键完成"沉淀 + 归档"，让记忆库保持健康：
- 沉淀（consolidate）：把经 QA 校验有效的事件记忆固化为长期知识，跨楼宇可复用、不随时间衰减。
- 归档（archive）：把超过保留期（默认 365 天）的事件记忆转入归档表，保持活跃检索集聚焦近期。

用法：
    python scripts/memory_maintenance.py              # 查看统计
    python scripts/memory_maintenance.py --run        # 执行沉淀 + 归档
    python scripts/memory_maintenance.py --run --retention 180   # 自定义保留期
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 确保能 import facilitymind（脚本可从仓库根目录直接运行）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from facilitymind.memory.store import get_store
from facilitymind.memory.decay import run_maintenance, consolidate, archive_old


def show_stats() -> dict:
    store = get_store()
    stats = store.stats()
    if stats.get("disabled"):
        print(f"[记忆库不可用] {stats.get('error', 'unknown')}")
        return stats
    print("=== 记忆库统计 ===")
    print(f"  事件记忆（episodic） : {stats.get('incidents', 0)} 条")
    print(f"  资产·知识（semantic）: {stats.get('asset_knowledge', 0)} 条")
    print(f"  沉淀知识（长期 KB）   : {stats.get('kb_learnings', 0)} 条")
    print(f"  已归档               : {stats.get('archived', 0)} 条")
    return stats


def main() -> int:
    p = argparse.ArgumentParser(description="FacilityMind 记忆层维护：沉淀 + 归档")
    p.add_argument("--run", action="store_true", help="执行沉淀 + 归档（默认只看统计）")
    p.add_argument("--consolidate", action="store_true", help="只执行沉淀")
    p.add_argument("--archive", action="store_true", help="只执行归档")
    p.add_argument("--retention", type=int, default=365, help="事件记忆保留期（天，默认 365）")
    p.add_argument("--min-support", type=int, default=1,
                   help="沉淀最小样本数（同根因出现 N 次才沉淀，默认 1）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = p.parse_args()

    store = get_store()
    if store.disabled:
        msg = {"disabled": True, "error": getattr(store, "_error", "unknown")}
        if args.json:
            print(json.dumps(msg, ensure_ascii=False))
        else:
            print(f"[记忆库不可用] {msg['error']}")
        return 1

    if not (args.run or args.consolidate or args.archive):
        stats = show_stats()
        if args.json:
            print(json.dumps(stats, ensure_ascii=False))
        return 0

    print("--- 维护前 ---")
    show_stats()

    do_consol = args.run or args.consolidate
    do_arch = args.run or args.archive
    result: dict = {}
    if do_consol:
        result["consolidate"] = consolidate(store=store, min_support=args.min_support)
    if do_arch:
        result["archive"] = archive_old(store=store, retention_days=args.retention)
    result["stats"] = store.stats()

    print("\n--- 维护后 ---")
    show_stats()
    print("\n=== 维护结果 ===")
    if "consolidate" in result:
        c = result["consolidate"]
        print(f"  沉淀：标记 {c.get('promoted', 0)} 条事件 → "
              f"新增 {c.get('kb_added', 0)} 条 / 更新 {c.get('kb_updated', 0)} 条长期知识 "
              f"（覆盖 {c.get('groups', 0)} 个经验模式）")
    if "archive" in result:
        a = result["archive"]
        print(f"  归档：{a.get('archived', 0)} 条超期事件转入归档表（cutoff={a.get('cutoff', '')}）")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
