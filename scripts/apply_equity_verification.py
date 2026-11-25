"""P1-M1-02 应用权益事件核验结果 (官方比例/分红) 到 `equity_events`.

输入: `scripts/verify_equity_events.py` 产出的 `equity_event_verification_*.json`
  - official_ratio: 拆分/合并 → ratio=官方 price_factor, verified=TRUE, inferred=FALSE
  - official_cash:  分红 → event_type=dividend_official, ratio 保留观测价格因子 (QC 用),
                    现金额写入 method JSON (研究价格用加法)
输出: 幂等 UPSERT + 应用报告 JSON; 默认 dry-run, `--apply` 才写库 (先备份).

流程 (与 rehearsal_v8 一致):
  1. 副本演练: --db <copy> --apply --no-backup
  2. 真实库:   --apply (自动备份 quant_pilot_<date>_pre_equity_apply.duckdb)
  3. 应用后复核: counts 稳定、verified=13、validator 无 target critical、重跑幂等

Usage:
    uv run python scripts/apply_equity_verification.py                 # dry-run
    uv run python scripts/apply_equity_verification.py --db <copy> --apply --no-backup
    uv run python scripts/apply_equity_verification.py --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import polars as pl  # noqa: E402

from data_pipeline.duckdb_manager import DuckDBManager  # noqa: E402
from data_pipeline.processors.validator import validate_ohlcv  # noqa: E402

BACKUP_DIR = PROJECT_ROOT / "data" / "backups"
DEFAULT_DB = PROJECT_ROOT / "data" / "quant_pilot.duckdb"
DEFAULT_VERIFICATION = BACKUP_DIR / "equity_event_verification_20261124_233330.json"
REPORT_PATH = BACKUP_DIR / "equity_apply_report.json"


def _latest_verification() -> Path:
    curated = BACKUP_DIR / "equity_event_verification_curated.json"
    if curated.exists():
        return curated
    candidates = sorted(BACKUP_DIR.glob("equity_event_verification_*.json"))
    if not candidates:
        raise SystemExit("未找到 equity_event_verification_*.json")
    return candidates[-1]


def _fingerprint(db: DuckDBManager) -> str:
    events = db.get_equity_events()
    rows = (
        events.select(
            ["symbol", "event_date", "event_type", "ratio", "source", "inferred", "verified"]
        ).sort(["symbol", "event_date"]).to_dicts()
        if not events.is_empty() else []
    )
    payload = json.dumps(rows, ensure_ascii=False, default=str, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _table_counts(db: DuckDBManager) -> dict[str, int]:
    tables = [row[0] for row in db.conn.execute("SHOW TABLES").fetchall()]
    counts: dict[str, int] = {}
    for table in sorted(tables):
        counts[table] = int(db.conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
    return counts


def _method_payload(item: dict) -> dict:
    announcement = item.get("announcement") or {}
    return {
        "announcement_id": announcement.get("id"),
        "announcement_date": announcement.get("date"),
        "announcement_title": announcement.get("title"),
        "official_ratio": item.get("official_ratio"),
        "cash_per_share": item.get("cash_per_share"),
        "inferred_ratio": item.get("inferred_ratio"),
        "verified_at": datetime.now().date().isoformat(),
        "evidence": "equity_event_verification",
    }


def build_rows(results: list[dict]) -> pl.DataFrame:
    rows: list[dict] = []
    for item in results:
        if item.get("status") not in ("official_ratio", "official_cash"):
            continue
        symbol = str(item["symbol"]).zfill(6)
        event_date = date.fromisoformat(str(item["event_date"]))
        official_ratio = item.get("official_ratio")
        cash = item.get("cash_per_share")
        if item["status"] == "official_cash":
            event_type = "dividend_official"
            ratio = float(item["inferred_ratio"])  # QC 用观测价格因子; 研究价格用 cash
            note = (
                f"官方分红 {cash:.4f} 元/份"
                f"（原登记 split_implied/ratio={item['inferred_ratio']}）; "
                "QC ratio 保留观测价格因子, 现金分红由研究价格按加法处理"
            )
        else:
            ratio = float(official_ratio)
            event_type = "merge_official" if ratio < 1 else "split_official"
            note = (
                f"官方{'合并' if ratio < 1 else '拆分'}比例 {ratio:g} "
                f"(原推断 {item['inferred_ratio']}; 含事件日市场波动)"
            )
        rows.append({
            "symbol": symbol,
            "event_date": event_date,
            "event_type": event_type,
            "ratio": ratio,
            "ratio_semantics": "price_factor",
            "source": item.get("source") or "official_announcement",
            "inferred": False,
            "verified": True,
            "method": json.dumps(_method_payload(item), ensure_ascii=False),
            "note": note,
        })
    return pl.DataFrame(rows)


def _backup_database(db: DuckDBManager, backup_path: Path) -> None:
    """DuckDB 原生一致性备份 (面板占用文件时 shutil.copy2 会 WinError 32)."""
    if backup_path.exists():
        backup_path.unlink()
    db.conn.execute(f"ATTACH '{backup_path.as_posix()}' AS _backup")
    try:
        current = db.conn.execute("SELECT current_database()").fetchone()[0]
        db.conn.execute(f'COPY FROM DATABASE "{current}" TO _backup')
    finally:
        db.conn.execute("DETACH _backup")


def _target_criticals(db: DuckDBManager, symbols: set[str]) -> list[str]:
    issues = validate_ohlcv(
        db.get_ohlcv(), registered_equity_events=db.get_equity_events(),
    )
    return [
        f"{issue.symbol}: {issue.check}: {issue.detail[:120]}"
        for issue in issues
        if issue.severity == "critical" and issue.symbol in symbols
    ]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="应用权益事件核验结果")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--verification", default=None)
    parser.add_argument("--apply", action="store_true", help="写库 (默认仅 dry-run)")
    parser.add_argument("--no-backup", action="store_true", help="演练副本时不备份")
    args = parser.parse_args()

    verification_path = Path(args.verification) if args.verification else _latest_verification()
    payload = json.loads(verification_path.read_text(encoding="utf-8"))
    rows = build_rows(payload["results"])
    if rows.is_empty():
        raise SystemExit("核验结果中没有可应用的条目")

    print(f"核验文件: {verification_path}")
    print(f"计划应用: {len(rows)} 条")
    for row in rows.sort(["symbol", "event_date"]).iter_rows(named=True):
        print(
            f"  {row['symbol']} {row['event_date']} {row['event_type']:17s} "
            f"ratio={row['ratio']:.8g} source={row['source']}"
        )

    db = DuckDBManager(args.db)
    try:
        before_events = db.get_equity_events()
        before_fp = _fingerprint(db)
        counts_before = _table_counts(db)
        print(f"\n应用前: equity_events={len(before_events)} 行, fingerprint={before_fp}")

        if not args.apply:
            print("\n[dry-run] 未写库; 加 --apply 执行。")
            return

        backup_path = None
        if not args.no_backup:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = BACKUP_DIR / f"quant_pilot_{stamp}_pre_equity_apply.duckdb"
            _backup_database(db, backup_path)
            print(f"备份: {backup_path}")

        symbols = set(rows["symbol"].to_list())
        with db.transaction():
            changed = db.upsert_equity_events(rows)
            criticals = _target_criticals(db, symbols)
            if criticals:
                raise RuntimeError(
                    "应用后目标标的存在 critical, 已回滚: " + "; ".join(criticals[:3])
                )

        after_events = db.get_equity_events()
        after_fp = _fingerprint(db)
        counts_after = _table_counts(db)
        verified = (
            int(after_events.filter(pl.col("verified")).height)
            if not after_events.is_empty() else 0
        )
        # equity_events 自身预期变化, 其余表必须稳定
        stable = {
            k: v for k, v in counts_before.items() if k != "equity_events"
        } == {
            k: v for k, v in counts_after.items() if k != "equity_events"
        }
        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "db": str(args.db),
            "verification": str(verification_path),
            "backup": str(backup_path) if backup_path else None,
            "upserted": changed,
            "rows_planned": len(rows),
            "equity_events_before": len(before_events),
            "equity_events_after": len(after_events),
            "verified_after": verified,
            "fingerprint_before": before_fp,
            "fingerprint_after": after_fp,
            "counts_stable": stable,
            "target_criticals": criticals,
        }
        REPORT_PATH.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(
            f"\n应用完成: upsert={changed}, equity_events={len(after_events)}, "
            f"verified={verified}"
        )
        print(f"counts 稳定: {stable}; 目标 critical: {len(criticals)}")
        print(f"报告: {REPORT_PATH}")
        if not stable:
            raise SystemExit("counts 不稳定, 请检查")
    finally:
        db.close()


if __name__ == "__main__":
    main()
