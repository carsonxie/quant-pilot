"""M7-01 隔离副本演练的回归测试 (plan §5 迁移保护 / §5.8 恢复路径).

在临时目录内复现演练动作 (不触碰真实库):
  - 迁移幂等 + 事实不变
  - 半笔事件回滚 (非法事件不留痕)
  - 重复导入去重
  - 备份恢复 + 新增事件重放 → 状态与事件指纹一致
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import polars as pl
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from rehearsal_m7 import (  # noqa: E402
    run_migration_drill,
    run_recovery_drill,
    run_rerun_drill,
    run_rollback_drill,
)

from data_pipeline.duckdb_manager import SCHEMA_VERSION, DuckDBManager  # noqa: E402


@pytest.fixture()
def source_db(tmp_path: Path) -> str:
    """临时"源库": 最小可迁移库 (行情 + 信号 + 空账户表)."""
    path = str(tmp_path / "source.duckdb")
    db = DuckDBManager(path)
    db.init_database()
    db.upsert_ohlcv(pl.DataFrame([{
        "symbol": "510300", "date": date(2026, 9, 11), "open": 4.5,
        "high": 4.6, "low": 4.4, "close": 4.58, "volume": 1e8, "amount": 4.58e8,
    }]))
    db.upsert_signals(pl.DataFrame([{
        "signal_id": 20260911001, "signal_date": date(2026, 9, 11),
        "symbol": "510300", "direction": "HOLD", "target_weight": 0.0,
        "trigger_reason": "演练", "strategy_version": "v0.1",
    }]))
    db.close()
    return path


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    d = tmp_path / "backups"
    d.mkdir()
    return d


class TestMigrationDrill:
    def test_migration_idempotent_and_facts_unchanged(self, source_db, workdir):
        report: dict = {}
        copy = run_migration_drill(source_db, report, workdir=workdir)
        drill = report["migration_drill"]
        assert drill["ok"] is True
        assert drill["counts_unchanged"] is True
        assert drill["signals_unchanged"] is True
        assert drill["versions"] == list(range(1, SCHEMA_VERSION + 1))
        assert copy.exists()

    def test_rollback_leaves_no_trace(self, source_db, workdir):
        report: dict = {}
        copy = run_migration_drill(source_db, report, workdir=workdir)
        run_rollback_drill(copy, report)
        drill = report["rollback_drill"]
        assert drill["ok"] is True
        assert drill["raised_account_error"] is True
        assert drill["no_new_event_row"] is True
        assert drill["state_unchanged"] is True

    def test_rerun_dedup(self, source_db, workdir):
        report: dict = {}
        copy = run_migration_drill(source_db, report, workdir=workdir)
        run_rerun_drill(copy, report)
        drill = report["rerun_drill"]
        assert drill["ok"] is True
        assert drill["second_inserted"] == 0
        assert drill["second_skipped"] == 2


class TestRecoveryDrill:
    def test_backup_restore_with_late_event(self, source_db, workdir):
        report: dict = {}
        run_recovery_drill(source_db, report, workdir=workdir)
        drill = report["recovery_drill"]
        assert drill["ok"] is True
        assert drill["event_fingerprint_match"] is True
        # 恢复后现金 = 初始 10000 + 迟到入金 5000
        assert drill["state_recovered"]["cash"] == pytest.approx(15000.0)
        assert drill["state_original"] == drill["state_recovered"]

    def test_recovery_default_paths_would_be_in_workdir(self, source_db, workdir):
        report: dict = {}
        run_recovery_drill(source_db, report, workdir=workdir)
        # 演练产物落在隔离目录 (不触碰真实 data/backups)
        assert Path(report["recovery_drill"]["backup"]).parent == workdir
        assert Path(report["recovery_drill"]["working"]).parent == workdir
