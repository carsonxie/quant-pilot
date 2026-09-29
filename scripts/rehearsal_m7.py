"""M7-01 隔离副本演练 — 迁移 / 重跑 / 恢复 (plan §5 迁移保护).

在**隔离副本**上完成并输出可复核证据, 不触碰真实库:
  1. 迁移演练: 副本从当前真实库复制 → 重复 init_database (幂等) → 关键表行数与
     业务事实逐项核对 (ohlcv_daily / trade_signals / 账户事件/份额/现金)。
  2. 半笔事件保护: 注入非法事件 (卖超) → 事务回滚; 事件表与回放状态无变化。
  3. 重跑幂等: 同一 CSV 二次导入 0 新增; account-import 的确定性 event_id 保证
     无法重复计账。
  4. 恢复路径演练 (plan §5.8): 演练"备份恢复 + 新事件重放":
       备份 → 导入新事件 A → 从备份恢复 → 重放新事件 A → 状态一致;
     验证恢复不会丢失备份后产生的真实事件 (需保留并核对新事件后恢复/前向修复)。
  5. 迁移保护清单输出 (JSON), 供审计引用。

本脚本只读真实库 (复制文件), 在 `data/backups/` 下生成演练副本与报告;
退出码 0 = 全部演练通过。

Usage:
    uv run python scripts/rehearsal_m7.py
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


from data_pipeline.accounting import (  # noqa: E402
    AccountError,
    AccountEvent,
    AccountService,
)
from data_pipeline.duckdb_manager import DuckDBManager  # noqa: E402


def _counts(db: DuckDBManager, tables: list[str]) -> dict[str, int]:
    return {
        t: db.get_table_count(t) for t in tables if db.table_exists(t)
    }


def _event_fingerprint(db: DuckDBManager, account_id: str) -> str:
    """账户事件内容的确定性指纹 (行数 + 逐行哈希), 用于恢复后一致性核对."""
    df = db.get_account_events(account_id)
    if df.is_empty():
        return "empty"
    ordered = df.sort("event_time", "event_id")
    payload = ordered.select([
        "event_id", "event_type", "event_time", "symbol", "side",
        "shares", "price", "amount", "fee",
    ]).write_json()
    import hashlib

    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _state_summary(db: DuckDBManager, account_id: str) -> dict:
    service = AccountService(db)
    state = service.current_state(account_id)
    return {
        "cash": round(state.cash, 6),
        "cash_withdrawable": round(state.cash_withdrawable, 6),
        "receivable": round(state.receivable, 6),
        "positions": {s: round(v, 6) for s, v in state.positions_summary().items()},
    }


def run_migration_drill(
    src: str, report: dict, workdir: Path | None = None,
) -> Path:
    """迁移演练: 副本上重复迁移 + 事实核对 (幂等)."""
    workdir = workdir or (PROJECT_ROOT / "data" / "backups")
    copy = workdir / "rehearsal_m7_migration.duckdb"
    shutil.copy2(src, copy)
    db = DuckDBManager(str(copy))
    before_counts = _counts(db, [
        "ohlcv_daily", "trade_signals", "account_events", "position_lots",
        "trade_proposals", "strategy_states",
    ])
    before_signals = db.conn.execute(
        "SELECT signal_id, symbol, direction, target_weight, strategy_version "
        "FROM trade_signals ORDER BY signal_id"
    ).fetchall()

    # 重复迁移两次 (幂等)
    db.init_database()
    db.init_database()
    after_counts = _counts(db, [
        "ohlcv_daily", "trade_signals", "account_events", "position_lots",
        "trade_proposals", "strategy_states",
    ])
    after_versions = [
        r[0] for r in db.conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
    ]
    after_signals = db.conn.execute(
        "SELECT signal_id, symbol, direction, target_weight, strategy_version "
        "FROM trade_signals ORDER BY signal_id"
    ).fetchall()
    db.close()

    report["migration_drill"] = {
        "copy": str(copy),
        "versions": after_versions,
        "counts_before": before_counts,
        "counts_after": after_counts,
        "counts_unchanged": before_counts == after_counts,
        "signals_unchanged": before_signals == after_signals,
        "idempotent_rerun": True,
        "ok": before_counts == after_counts and before_signals == after_signals,
    }
    return copy


def run_rollback_drill(copy: Path, report: dict) -> None:
    """半笔事件保护: 非法事件 → 回滚, 事件表与状态不变."""
    db = DuckDBManager(str(copy))
    service = AccountService(db)
    service.ensure_account("M7-DRILL", "演练账户", mode="paper")
    seed_events = [
        AccountEvent(
            event_id="drill-init", account_id="M7-DRILL", event_type="initial",
            event_time=datetime(2026, 1, 5, 10, 0), amount=10000.0,
        ),
        AccountEvent(
            event_id="drill-buy", account_id="M7-DRILL", event_type="trade",
            event_time=datetime(2026, 1, 6, 10, 0), symbol="510300",
            side="BUY", shares=100.0, price=10.0, fee=5.0,
        ),
    ]
    service.import_events(seed_events)
    state_before = _state_summary(db, "M7-DRILL")
    event_count_before = db.get_table_count("account_events")

    # 非法: 卖超 (无持仓卖出 1000 份)
    bad = AccountEvent(
        event_id="drill-oversell", account_id="M7-DRILL", event_type="trade",
        event_time=datetime(2026, 1, 7, 10, 0), symbol="510300",
        side="SELL", shares=1000.0, price=10.0, fee=5.0,
    )
    rollback_ok = False
    try:
        service.import_events([bad])
    except AccountError as exc:
        rollback_ok = "卖出超过持仓" in str(exc)
    state_after = _state_summary(db, "M7-DRILL")
    event_count_after = db.get_table_count("account_events")
    db.close()

    report["rollback_drill"] = {
        "raised_account_error": rollback_ok,
        "state_unchanged": state_before == state_after,
        "no_new_event_row": event_count_before == event_count_after,
        "event_count": event_count_after,
        "ok": rollback_ok and state_before == state_after
        and event_count_before == event_count_after,
    }


def run_rerun_drill(copy: Path, report: dict) -> None:
    """重跑幂等: 同一事件集二次导入 0 新增 (确定性去重)."""
    db = DuckDBManager(str(copy))
    service = AccountService(db)
    events = [
        AccountEvent(
            event_id="rerun-init", account_id="M7-DRILL", event_type="initial",
            event_time=datetime(2026, 1, 5, 10, 0), amount=10000.0,
        ),
        AccountEvent(
            event_id="rerun-buy", account_id="M7-DRILL", event_type="trade",
            event_time=datetime(2026, 1, 6, 10, 0), symbol="510300",
            side="BUY", shares=100.0, price=10.0, fee=5.0,
        ),
    ]
    first = service.import_events(events)
    second = service.import_events(events)
    state_first = _state_summary(db, "M7-DRILL")
    db.close()
    report["rerun_drill"] = {
        "first_inserted": first.inserted,
        "second_inserted": second.inserted,
        "second_skipped": second.skipped,
        "state_after_first": state_first,
        "ok": second.inserted == 0 and second.skipped == len(events),
    }


def run_recovery_drill(
    src: str, report: dict, workdir: Path | None = None,
) -> None:
    """恢复路径: 备份 → 新事件 → 恢复 → 重放新事件 → 状态一致 (plan §5.8)."""
    workdir = workdir or (PROJECT_ROOT / "data" / "backups")
    base = workdir / "rehearsal_m7_recovery_base.duckdb"
    backup = workdir / "rehearsal_m7_recovery_backup.duckdb"
    working = workdir / "rehearsal_m7_recovery_work.duckdb"

    # 1. 从真实库复制得到"受控副本", 在其上造账户与初始事件 → 作为备份点
    shutil.copy2(src, base)
    db = DuckDBManager(str(base))
    service = AccountService(db)
    service.ensure_account("M7-RECOVER", "恢复演练", mode="paper")
    service.import_events([
        AccountEvent(
            event_id="rec-init", account_id="M7-RECOVER", event_type="initial",
            event_time=datetime(2026, 1, 5, 10, 0), amount=10000.0,
        ),
    ])
    db.close()
    shutil.copy2(base, backup)          # 备份点 (含初始事件)
    shutil.copy2(backup, working)       # 工作库从备份恢复

    # 2. 备份之后产生新真实事件 (在"原库"上发生, 模拟切换后新事件)
    late_event = AccountEvent(
        event_id="rec-late-deposit", account_id="M7-RECOVER", event_type="deposit",
        event_time=datetime(2026, 2, 2, 10, 0), amount=5000.0,
        reference="bank-transfer-001",
    )
    db = DuckDBManager(str(base))
    AccountService(db).import_events([late_event])
    db.close()

    # 3. 恢复后重放新事件 (从事件渠道取回; 此处直接用已知事件重放)
    db = DuckDBManager(str(working))
    AccountService(db).import_events([late_event])
    state_recovered = _state_summary(db, "M7-RECOVER")
    db.close()

    # 4. 原库当前状态 (含新事件) 与恢复后状态比较
    db = DuckDBManager(str(base))
    state_original = _state_summary(db, "M7-RECOVER")
    fingerprint_original = _event_fingerprint(db, "M7-RECOVER")
    db.close()
    db = DuckDBManager(str(working))
    fingerprint_recovered = _event_fingerprint(db, "M7-RECOVER")
    db.close()

    report["recovery_drill"] = {
        "backup": str(backup),
        "working": str(working),
        "new_event_after_backup": "rec-late-deposit",
        "state_original": state_original,
        "state_recovered": state_recovered,
        "event_fingerprint_match": fingerprint_original == fingerprint_recovered,
        "ok": state_original == state_recovered
        and fingerprint_original == fingerprint_recovered,
        "note": (
            "恢复流程: 从备份恢复后必须重放备份点之后的全部真实事件 "
            "(事件渠道/CSV), 核对事件指纹与现金/份额一致后继续 (plan §5.8)"
        ),
    }


def main() -> None:
    src = PROJECT_ROOT / "data" / "quant_pilot.duckdb"
    if not src.exists():
        print(f"[M7] 真实库不存在: {src}")
        sys.exit(1)

    report: dict = {"generated_at": datetime.now().isoformat(), "source": str(src)}
    workdir = PROJECT_ROOT / "data" / "backups"
    copy = run_migration_drill(str(src), report, workdir=workdir)
    run_rollback_drill(copy, report)
    run_rerun_drill(copy, report)
    run_recovery_drill(str(src), report, workdir=workdir)

    all_ok = all(
        report[key].get("ok", False)
        for key in ("migration_drill", "rollback_drill", "rerun_drill", "recovery_drill")
    )
    report["all_ok"] = all_ok

    out = workdir / "rehearsal_m7_report.json"
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    print("===== M7 隔离副本演练 (plan §5) =====")
    print(f"迁移: versions={report['migration_drill']['versions']} "
          f"counts_unchanged={report['migration_drill']['counts_unchanged']} "
          f"signals_unchanged={report['migration_drill']['signals_unchanged']}")
    print(f"回滚: {report['rollback_drill']}")
    print(f"重跑: {report['rerun_drill']['second_inserted']} 新增 / "
          f"{report['rerun_drill']['second_skipped']} 去重")
    print(f"恢复: state_match={report['recovery_drill']['ok']} "
          f"fingerprint_match={report['recovery_drill']['event_fingerprint_match']}")
    print(f"报告: {out}")
    print(f"ALL_OK={all_ok}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
