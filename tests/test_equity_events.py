"""权益事件登记与拆分感知质检 (M8/I-09 方案1) 单元测试 — 独立期望值.

覆盖:
  - 已登记跳变: 日期 + 比例一致 → registered_equity_event (info, 不阻断)
  - 未登记跳变 → 维持 critical (隔离设计不变)
  - 比例不符 (如登记 1.5 但实际 ÷2) → 仍 critical (警惕二次修订)
  - 未超限的已登记日 → 不产生放行/提示 (无需按登记处理)
  - 反向折算 (ratio < 1, 份额合并) 同样可登记
  - 登记表持久化幂等; 登记脚本的检测为产品感知 (20% 板不误报)
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import polars as pl
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from data_pipeline.duckdb_manager import DuckDBManager  # noqa: E402
from data_pipeline.processors.validator import validate_ohlcv  # noqa: E402


def _two_day(symbol: str, close_prev: float, close_curr: float) -> pl.DataFrame:
    return pl.DataFrame([
        {"symbol": symbol, "date": date(2026, 1, 1), "open": close_prev,
         "high": close_prev, "low": close_prev, "close": close_prev,
         "volume": 1e8, "amount": 1e8},
        {"symbol": symbol, "date": date(2026, 1, 2), "open": close_curr,
         "high": close_curr, "low": close_curr, "close": close_curr,
         "volume": 1e8, "amount": 1e8},
    ])


def _events(*pairs: tuple[str, date, float]) -> pl.DataFrame:
    return pl.DataFrame([
        {"symbol": s, "event_date": d, "ratio": r} for s, d, r in pairs
    ])


class TestSplitAwareValidation:
    def test_registered_split_is_not_blocking(self):
        """独立期望: 登记 ÷2.0 → 期望变动 −50%; 实际 −50% → info 不阻断."""
        df = _two_day("512480", 2.0, 1.0)  # −50%
        events = _events(("512480", date(2026, 1, 2), 2.0))
        issues = validate_ohlcv(df, registered_equity_events=events)
        assert not [i for i in issues if i.severity == "critical"]
        assert [i.check for i in issues if i.severity == "info"] == [
            "registered_equity_event"
        ]

    def test_unregistered_jump_still_critical(self):
        """未登记: 同一跳变仍为 critical (隔离设计不变)."""
        df = _two_day("512480", 2.0, 1.0)
        issues = validate_ohlcv(df, registered_equity_events=_events())
        crits = [i for i in issues if i.severity == "critical"]
        assert len(crits) == 1
        assert crits[0].check == "extreme_daily_change"

    def test_ratio_mismatch_still_critical(self):
        """比例不符: 登记 ÷1.5 (期望 −33.3%) 但实际 −50% → critical."""
        df = _two_day("512480", 2.0, 1.0)
        events = _events(("512480", date(2026, 1, 2), 1.5))
        issues = validate_ohlcv(df, registered_equity_events=events)
        assert any(i.severity == "critical" for i in issues)

    def test_registered_within_limit_no_info(self):
        """已登记但实际未超限 (正常波动): 不产生放行提示."""
        df = _two_day("512480", 2.0, 1.9)  # −5%: 未超限
        events = _events(("512480", date(2026, 1, 2), 2.0))
        issues = validate_ohlcv(df, registered_equity_events=events)
        assert issues == []

    def test_reverse_consolidation_registered(self):
        """反向折算 (份额合并): ratio=0.311 → 期望 +221%; 实际 +221% → info."""
        # 独立期望: 0.497 → 1.597 = +221.33%
        df = _two_day("516160", 0.497, 1.597)
        events = _events(("516160", date(2026, 1, 2), 0.497 / 1.597))
        issues = validate_ohlcv(df, registered_equity_events=events)
        assert not [i for i in issues if i.severity == "critical"]
        assert any(i.check == "registered_equity_event" for i in issues)

    def test_tolerance_absorbs_data_noise(self):
        """比例一致性容差: 登记 3.918, 实际 −74.5% (理论 −74.47%) → 放行."""
        df = _two_day("159928", 1.261 * 3.918, 1.261)
        events = _events(("159928", date(2026, 1, 2), 3.918))
        issues = validate_ohlcv(df, registered_equity_events=events)
        assert not [i for i in issues if i.severity == "critical"]

    def test_spurious_registration_does_not_mask_other_dates(self):
        """登记其他日期不掩盖本次跳变 (日期必须精确匹配)."""
        df = _two_day("512480", 2.0, 1.0)
        events = _events(("512480", date(2025, 12, 31), 2.0))
        issues = validate_ohlcv(df, registered_equity_events=events)
        assert any(i.severity == "critical" for i in issues)


class TestEquityEventPersistence:
    @pytest.fixture()
    def db(self, tmp_path):
        db = DuckDBManager(str(tmp_path / "ee.duckdb"))
        db.init_database()
        yield db
        db.close()

    def test_upsert_idempotent_and_verified_flag(self, db):
        payload = pl.DataFrame([{
            "symbol": "513100", "event_date": date(2022, 1, 14), "ratio": 5.115,
            "method": "前收/当收",
        }])
        db.upsert_equity_events(payload)
        db.upsert_equity_events(payload)
        assert db.get_table_count("equity_events") == 1
        row = db.get_equity_events().row(0, named=True)
        assert row["ratio"] == pytest.approx(5.115)
        assert row["ratio_semantics"] == "price_factor"
        assert row["inferred"] is True and row["verified"] is False
        # 核验升级不重复插入
        db.upsert_equity_events(pl.DataFrame([{
            "symbol": "513100", "event_date": date(2022, 1, 14), "ratio": 5.115,
            "verified": True, "source": "official",
        }]))
        assert db.get_table_count("equity_events") == 1
        updated = db.get_equity_events().row(0, named=True)
        assert updated["verified"] is True and updated["source"] == "official"


class TestRegistrationScript:
    def test_detect_jumps_product_aware(self, tmp_path):
        """20% 板产品 ±20% 真实移动不视为跳变 (产品感知)."""
        from register_equity_events import detect_jumps

        path = str(tmp_path / "jumps.duckdb")
        db = DuckDBManager(path)
        db.init_database()
        rows = []
        # 159915 (20% 板): +20% 真实移动; 512480: ÷2 拆分跳变
        for symbol, closes in (
            ("159915", [1.0, 1.2]),
            ("512480", [2.0, 1.0]),
        ):
            for i, close in enumerate(closes):
                rows.append({
                    "symbol": symbol, "date": date(2026, 1, 1 + i),
                    "open": close, "high": close, "low": close, "close": close,
                    "volume": 1e8, "amount": 1e8,
                })
        db.upsert_ohlcv(pl.DataFrame(rows))
        jumps = detect_jumps(db, price_limits={"159915": 0.20})
        found = {(r["symbol"], r["event_date"]) for r in jumps.iter_rows(named=True)}
        assert ("512480", date(2026, 1, 2)) in found
        assert not any(sym == "159915" for sym, _ in found)
        db.close()

    def test_register_rolls_back_on_residual_critical(self, tmp_path):
        """登记复核失败 → 回滚 (同一标的残余 critical 不落库).

        构造: 单个标的同日有两个跳变时无法区分; 改用"同一标的两次跳变,
        只登记其一"且两次跳变比例不同 —— 未登记那次仍 critical → 回滚。
        """
        from register_equity_events import register

        path = str(tmp_path / "rollback.duckdb")
        db = DuckDBManager(path)
        db.init_database()
        rows = []
        # 512480: 两段跳变 (÷2.0 与 ÷1.5), 只登记第一段
        for i, close in enumerate([2.0, 1.0, 0.667]):
            rows.append({
                "symbol": "512480", "date": date(2026, 1, 1 + i),
                "open": close, "high": close, "low": close, "close": close,
                "volume": 1e8, "amount": 1e8,
            })
        db.upsert_ohlcv(pl.DataFrame(rows))
        with pytest.raises(ValueError, match="复核失败"):
            register(db, [{
                "symbol": "512480", "event_date": date(2026, 1, 2),
                "ratio": 2.0, "note": "测试",
            }])
        # 回滚: 登记表为空
        assert db.get_table_count("equity_events") == 0
        # 两段都登记 → 通过
        register(db, [
            {"symbol": "512480", "event_date": date(2026, 1, 2), "ratio": 2.0},
            {"symbol": "512480", "event_date": date(2026, 1, 3), "ratio": 1.5},
        ])
        assert db.get_table_count("equity_events") == 2
        db.close()
