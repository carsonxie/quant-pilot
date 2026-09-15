"""账户账本 (M2) 单元测试.

覆盖: 事件回放 (初始/出入金/成交/费用/分红/份额变动/更正)、批次可卖与现金口径、
重复导入去重、事务回滚 (不留半笔)、账户隔离、标准 CSV 导入.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from data_pipeline.accounting import (
    AccountError,
    AccountEvent,
    AccountService,
    read_events_csv,
    replay_account,
)
from data_pipeline.duckdb_manager import DuckDBManager


def dt(y: int, m: int, d: int, hh: int = 10, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm)


def ev(event_id: str, etype: str, when: datetime, **kwargs) -> AccountEvent:
    return AccountEvent(
        event_id=event_id,
        account_id=kwargs.pop("account_id", "A1"),
        event_type=etype,
        event_time=when,
        **kwargs,
    )


# ------------------------------------------------------------------
# 回放: 基础与现金口径
# ------------------------------------------------------------------

class TestReplayBasics:
    def test_initial_deposit_withdraw(self):
        events = [
            ev("e1", "initial", dt(2026, 9, 1), amount=100000.0),
            ev("e2", "deposit", dt(2026, 9, 2), amount=20000.0),
            ev("e3", "withdrawal", dt(2026, 9, 3), amount=5000.0),
        ]
        state = replay_account(events)
        assert state.cash == pytest.approx(115000.0)
        assert state.cash_withdrawable == pytest.approx(115000.0)
        assert state.last_event_id == "e3"

    def test_withdraw_beyond_available_rejected(self):
        events = [
            ev("e1", "initial", dt(2026, 9, 1), amount=1000.0),
            ev("e2", "withdrawal", dt(2026, 9, 2), amount=1500.0),
        ]
        with pytest.raises(AccountError, match="出金超过可取现金"):
            replay_account(events)

    def test_initial_with_positions(self):
        events = [ev(
            "e1", "initial", dt(2026, 9, 1), amount=50000.0,
            payload={"positions": [{
                "symbol": "510300", "shares": 10000, "cost_basis": 40000.0,
                "sellable_date": "2026-09-01",
            }]},
        )]
        state = replay_account(events)
        assert state.cash == pytest.approx(50000.0)
        assert state.shares_of("510300") == 10000
        lot = state.lots["510300"][0]
        assert lot.cost_basis == pytest.approx(40000.0)
        assert lot.sellable_date == date(2026, 9, 1)


# ------------------------------------------------------------------
# 成交与批次
# ------------------------------------------------------------------

class TestTrades:
    def test_buy_creates_lot_with_fee(self):
        events = [
            ev("e1", "initial", dt(2026, 9, 1), amount=100000.0),
            ev("e2", "trade", dt(2026, 9, 2, 9, 35), symbol="510300", side="BUY",
               shares=1000, price=4.0, fee=3.0),
        ]
        state = replay_account(events)
        assert state.cash == pytest.approx(100000.0 - 4003.0)
        lot = state.lots["510300"][0]
        assert lot.shares == 1000
        assert lot.cost_basis == pytest.approx(4003.0)
        assert lot.sellable_date == date(2026, 9, 3)  # T+1 (9/2 周三 → 9/3 周四)

    def test_buy_beyond_cash_rejected(self):
        events = [
            ev("e1", "initial", dt(2026, 9, 1), amount=1000.0),
            ev("e2", "trade", dt(2026, 9, 2), symbol="510300", side="BUY",
               shares=1000, price=4.0, fee=0.0),
        ]
        with pytest.raises(AccountError, match="买入现金不足"):
            replay_account(events)

    def test_sell_withdrawable_next_day(self):
        events = [
            ev("e1", "initial", dt(2026, 9, 1), amount=100000.0),
            ev("e2", "trade", dt(2026, 9, 2), symbol="510300", side="BUY",
               shares=1000, price=4.0, fee=0.0),
            ev("e3", "trade", dt(2026, 9, 3), symbol="510300", side="SELL",
               shares=1000, price=4.2, fee=2.1),
            ev("e4", "dividend_accrual", dt(2026, 9, 4), symbol="510300", amount=1.0),
        ]
        state = replay_account(events)
        expected = 96000.0 + 4200.0 - 2.1
        assert state.cash == pytest.approx(expected)
        assert state.cash_withdrawable == pytest.approx(expected)  # 跨日后转可取
        assert state.pending_withdrawable == pytest.approx(0.0)
        assert state.shares_of("510300") == 0

    def test_sell_proceeds_available_but_not_withdrawable_same_day(self):
        events = [
            ev("e1", "initial", dt(2026, 9, 1), amount=100000.0),
            ev("e2", "trade", dt(2026, 9, 2), symbol="510300", side="BUY",
               shares=1000, price=4.0, fee=0.0),
            ev("e3", "trade", dt(2026, 9, 3), symbol="510300", side="SELL",
               shares=1000, price=4.2, fee=2.1),
        ]
        state = replay_account(events)
        assert state.cash == pytest.approx(96000.0 + 4197.9)
        assert state.cash_withdrawable == pytest.approx(96000.0)
        assert state.pending_withdrawable == pytest.approx(4197.9)

    def test_oversell_rejected(self):
        events = [
            ev("e1", "initial", dt(2026, 9, 1), amount=100000.0),
            ev("e2", "trade", dt(2026, 9, 2), symbol="510300", side="BUY",
               shares=100, price=4.0, fee=0.0),
            ev("e3", "trade", dt(2026, 9, 3), symbol="510300", side="SELL",
               shares=200, price=4.0, fee=0.0),
        ]
        with pytest.raises(AccountError, match="卖出超过持仓"):
            replay_account(events)

    def test_fifo_lot_consumption(self):
        events = [
            ev("e1", "initial", dt(2026, 9, 1), amount=100000.0),
            ev("e2", "trade", dt(2026, 9, 2), symbol="X", side="BUY",
               shares=100, price=1.0, fee=0.0),
            ev("e3", "trade", dt(2026, 9, 3), symbol="X", side="BUY",
               shares=100, price=2.0, fee=0.0),
            ev("e4", "trade", dt(2026, 9, 4), symbol="X", side="SELL",
               shares=150, price=3.0, fee=0.0),
        ]
        state = replay_account(events)
        lots = state.lots["X"]
        assert len(lots) == 1
        assert lots[0].shares == pytest.approx(50.0)
        assert lots[0].cost_basis == pytest.approx(100.0)  # 200×50%


# ------------------------------------------------------------------
# 分红与份额变动
# ------------------------------------------------------------------

class TestDividends:
    def test_accrual_then_payment_no_double_count(self):
        events = [
            ev("e1", "initial", dt(2026, 9, 1), amount=1000.0),
            ev("e2", "dividend_accrual", dt(2026, 9, 2), symbol="510300", amount=50.0),
            ev("e3", "dividend_payment", dt(2026, 9, 3), symbol="510300", amount=50.0),
            ev("e4", "dividend_accrual", dt(2026, 9, 10), symbol="510300", amount=1.0),
        ]
        state = replay_account(events)
        assert state.cash == pytest.approx(1050.0)          # 1000 + 到账 50
        assert state.cash_withdrawable == pytest.approx(1050.0)
        assert state.receivable == pytest.approx(1.0)       # 新应收, 不重复计资产

    def test_payment_without_accrual_direct(self):
        events = [
            ev("e1", "initial", dt(2026, 9, 1), amount=1000.0),
            ev("e2", "dividend_payment", dt(2026, 9, 2), symbol="510300", amount=30.0),
        ]
        state = replay_account(events)
        assert state.receivable == pytest.approx(0.0)
        assert state.cash == pytest.approx(1030.0)


class TestShareChange:
    def test_split_adjusts_lots_cost_unchanged(self):
        events = [
            ev("e1", "initial", dt(2026, 9, 1), amount=100000.0,
               payload={"positions": [{
                   "symbol": "512480", "shares": 1000,
                   "cost_basis": 2000.0, "sellable_date": "2026-09-01",
               }]}),
            ev("e2", "share_change", dt(2026, 9, 2), symbol="512480",
               payload={"delta_shares": 1000.0}),  # 1 拆 2
        ]
        state = replay_account(events)
        lot = state.lots["512480"][0]
        assert lot.shares == pytest.approx(2000.0)
        assert lot.cost_basis == pytest.approx(2000.0)
        assert state.cash == pytest.approx(100000.0)


# ------------------------------------------------------------------
# 更正
# ------------------------------------------------------------------

class TestCorrections:
    def test_correction_replaces_replayed_value(self):
        events = [
            ev("e1", "initial", dt(2026, 9, 1), amount=100000.0),
            ev("b1", "trade", dt(2026, 9, 2), symbol="510300", side="BUY",
               shares=1000, price=3.0, fee=0.0),
            ev("s1", "trade", dt(2026, 9, 3), symbol="510300", side="SELL",
               shares=1000, price=4.0, fee=0.0),
        ]
        assert replay_account(events).cash == pytest.approx(101000.0)

        correction = ev(
            "c1", "correction", dt(2026, 9, 5), correction_of="s1",
            symbol="510300", side="SELL", shares=1000, price=4.5, fee=0.0,
        )
        state = replay_account(events + [correction])
        assert state.cash == pytest.approx(101500.0)  # 更正后卖出价 4.5
        # 更正不改变事件时间 (原 9/3)
        assert state.last_event_time == dt(2026, 9, 3)

    def test_correction_of_missing_event_raises(self):
        events = [
            ev("e1", "initial", dt(2026, 9, 1), amount=1000.0),
            ev("c1", "correction", dt(2026, 9, 2),
               correction_of="ghost", amount=10.0),
        ]
        with pytest.raises(AccountError, match="不存在的事件"):
            replay_account(events)


# ------------------------------------------------------------------
# 服务: 导入幂等 / 事务回滚 / 账户隔离
# ------------------------------------------------------------------

@pytest.fixture()
def svc(tmp_path: Path):
    db = DuckDBManager(str(tmp_path / "acc.duckdb"))
    db.init_database()
    service = AccountService(db)
    service.ensure_account("T-1", "测试账户", mode="paper")
    yield service
    db.close()


def _base_events(account_id: str = "T-1") -> list[AccountEvent]:
    return [
        ev("e1", "initial", dt(2026, 9, 1), account_id=account_id, amount=100000.0),
        ev("e2", "trade", dt(2026, 9, 2), account_id=account_id, symbol="510300",
           side="BUY", shares=1000, price=4.0, fee=3.0),
    ]


class TestServiceImport:
    def test_import_inserts_and_rebuilds_derived(self, svc):
        result = svc.import_events(_base_events())
        assert result.inserted == 2 and result.skipped == 0
        assert result.state.cash == pytest.approx(95997.0)

        lots = svc.db.get_position_lots("T-1")
        assert lots.height == 1
        assert lots["symbol"][0] == "510300"
        assert lots["shares"][0] == pytest.approx(1000.0)

        snap = svc.latest_snapshot("T-1")
        assert snap is not None
        assert snap["cash"] == pytest.approx(95997.0)
        assert snap["event_cutoff"] == "e2"

    def test_duplicate_import_no_second_event(self, svc):
        first = svc.import_events(_base_events())
        second = svc.import_events(_base_events())
        assert first.inserted == 2
        assert second.inserted == 0 and second.skipped == 2
        assert svc.db.get_table_count("account_events") == 2
        assert second.state.cash == pytest.approx(first.state.cash)

    def test_invalid_event_rolls_back(self, svc):
        """卖超事件 → 事务回滚, 不留半笔 (plan §5)."""
        svc.import_events(_base_events())
        bad = [ev("e3", "trade", dt(2026, 9, 3), account_id="T-1",
                  symbol="510300", side="SELL", shares=9999, price=4.0, fee=0.0)]
        with pytest.raises(AccountError, match="卖出超过持仓"):
            svc.import_events(bad)
        assert svc.db.get_table_count("account_events") == 2  # e3 未写入
        assert svc.current_state("T-1").cash == pytest.approx(95997.0)

    def test_account_isolation(self, svc):
        svc.ensure_account("T-2", "模拟账户", mode="backtest")
        svc.import_events(_base_events("T-1"))
        svc.import_events([
            ev("x1", "initial", dt(2026, 9, 1), account_id="T-2", amount=5000.0),
        ])
        assert svc.current_state("T-1").cash == pytest.approx(95997.0)
        assert svc.current_state("T-2").cash == pytest.approx(5000.0)
        assert svc.current_state("T-2").shares_of("510300") == 0.0

    def test_mixed_account_import_rejected(self, svc):
        with pytest.raises(AccountError, match="同一账户"):
            svc.import_events(
                _base_events("T-1")
                + [ev("x1", "initial", dt(2026, 9, 1), account_id="T-2", amount=1.0)]
            )


# ------------------------------------------------------------------
# 标准 CSV 导入
# ------------------------------------------------------------------

class TestCsvImport:
    def test_csv_roundtrip_and_dedup(self, svc, tmp_path: Path):
        csv_path = tmp_path / "events.csv"
        csv_path.write_text(
            "event_time,event_type,symbol,side,shares,price,amount,fee,source,reference\n"
            "2026-09-01 10:00:00,initial,,,,,100000,0,csv,EVT-INIT\n"
            "2026-09-02 09:35:00,trade,510300,BUY,1000,4.10,,2.05,csv,T001\n",
            encoding="utf-8",
        )
        events = read_events_csv(csv_path, account_id="T-1")
        assert len(events) == 2
        assert events[0].event_id == "EVT-INIT"   # 外部编号作为去重 ID
        assert events[1].event_id == "T001"

        r1 = svc.import_events(events)
        r2 = svc.import_events(read_events_csv(csv_path, account_id="T-1"))
        assert r1.inserted == 2
        assert r2.inserted == 0 and r2.skipped == 2
        assert r1.state.cash == pytest.approx(100000.0 - 4100.0 - 2.05)

    def test_csv_deterministic_id_without_reference(self, tmp_path: Path):
        csv_path = tmp_path / "e2.csv"
        csv_path.write_text(
            "event_time,event_type,amount\n"
            "2026-09-01 10:00:00,deposit,500\n",
            encoding="utf-8",
        )
        first = read_events_csv(csv_path, account_id="T-9")
        second = read_events_csv(csv_path, account_id="T-9")
        assert first[0].event_id == second[0].event_id
        assert first[0].event_id.startswith("T-9-")

    def test_csv_bad_event_type_raises(self, tmp_path: Path):
        csv_path = tmp_path / "bad.csv"
        csv_path.write_text(
            "event_time,event_type,amount\n"
            "2026-09-01 10:00:00,mystery,500\n",
            encoding="utf-8",
        )
        with pytest.raises(AccountError, match="未知 event_type"):
            read_events_csv(csv_path, account_id="T-1")

    def test_docs_template_parses(self):
        """docs/account_events_template.csv 必须可解析 (10 条, 含更正与份额变动)."""
        template = (
            Path(__file__).resolve().parents[1]
            / "docs" / "account_events_template.csv"
        )
        events = read_events_csv(template, account_id="TEMPLATE")
        assert len(events) == 10
        types = [event.event_type for event in events]
        assert "correction" in types and "share_change" in types
        assert events[0].amount == pytest.approx(100000.0)
        assert events[2].side == "BUY"
        assert events[2].price == pytest.approx(4.10)
