"""Phase 1.1 schema v8 迁移演练与备份 (P11-M2-02; plan §5).

流程:
  1. 备份真实库 → data/backups/quant_pilot_<date>_pre_v8.duckdb
  2. 复制副本 → data/backups/rehearsal_v8.duckdb
  3. 副本上 init_database 两次 (幂等) → 迁移前后 counts / schema_version 核对
  4. --apply: 演练通过后应用到真实库 (只增表, 不改既有事实) 并核对 counts

输出报告: data/backups/rehearsal_v8_report.json
退出码 0 = 演练通过 (--apply 时表示已应用且核对通过)。

Usage:
    uv run python scripts/rehearsal_v8.py            # 仅演练 (不触碰真实库内容)
    uv run python scripts/rehearsal_v8.py --apply    # 应用真实库
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from data_pipeline.duckdb_manager import SCHEMA_VERSION, DuckDBManager  # noqa: E402

NEW_TABLES = [
    "securities", "watchlist", "watchlist_changes",
    "diag_price_daily", "diagnostic_snapshots",
]


def _existing_tables(db: DuckDBManager) -> list[str]:
    rows = db.conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()
    return [r[0] for r in rows]


def _counts(db: DuckDBManager, tables: list[str]) -> dict[str, int]:
    return {t: db.get_table_count(t) for t in tables}


def _versions(db: DuckDBManager) -> list[int]:
    return [
        r[0] for r in db.conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
    ]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="schema v8 迁移演练 (P11-M2-02)")
    parser.add_argument("--apply", action="store_true", help="演练通过后应用到真实库")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "quant_pilot.duckdb"))
    args = parser.parse_args()

    real_db_path = Path(args.db)
    backup_dir = PROJECT_ROOT / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    report: dict = {"generated_at": datetime.now().isoformat(timespec="seconds")}

    # 1) 备份真实库
    backup_path = backup_dir / f"quant_pilot_{stamp}_pre_v8.duckdb"
    shutil.copy2(real_db_path, backup_path)
    report["backup"] = str(backup_path)

    # 2) 副本
    rehearsal_path = backup_dir / "rehearsal_v8.duckdb"
    shutil.copy2(real_db_path, rehearsal_path)
    rehearsal = DuckDBManager(str(rehearsal_path))
    rehearsal.init_database()

    before_tables = _existing_tables(rehearsal)  # v8 后的表集合
    legacy_tables = [t for t in before_tables if t not in NEW_TABLES]
    counts_before = _counts(rehearsal, legacy_tables)
    versions_before = _versions(rehearsal)

    rehearsal.init_database()  # 二次执行 (幂等)
    counts_after = _counts(rehearsal, legacy_tables)
    versions_after = _versions(rehearsal)

    rehearsal_ok = (
        counts_before == counts_after
        and versions_before == versions_after
        and versions_after[-1] == SCHEMA_VERSION
        == 8
        and all(rehearsal.table_exists(t) for t in NEW_TABLES)
    )
    report["rehearsal"] = {
        "ok": rehearsal_ok,
        "versions": versions_after,
        "legacy_tables": len(legacy_tables),
        "counts_stable": counts_before == counts_after,
        "new_tables_empty": {
            t: rehearsal.get_table_count(t) for t in NEW_TABLES
        },
    }
    rehearsal.close()

    # 3) 应用真实库 (仅演练通过且 --apply)
    applied = False
    if args.apply and rehearsal_ok:
        real = DuckDBManager(str(real_db_path))
        real.init_database()
        real_tables = _existing_tables(real)
        real_versions = _versions(real)
        real_counts = _counts(real, [t for t in real_tables if t not in NEW_TABLES])
        applied_ok = (
            real_counts == counts_before
            and real_versions == versions_after
            and all(real.table_exists(t) for t in NEW_TABLES)
        )
        report["apply"] = {
            "ok": applied_ok,
            "versions": real_versions,
            "counts_stable": real_counts == counts_before,
        }
        real.close()
        applied = applied_ok
    elif not args.apply:
        report["apply"] = {"ok": None, "note": "未指定 --apply, 仅演练"}

    report_path = backup_dir / "rehearsal_v8_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n报告: {report_path}")
    ok = rehearsal_ok and (applied if args.apply else True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
