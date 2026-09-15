"""建议与执行反馈账本 (M2b) 单元测试.

覆盖: 建议去重、部分成交状态机、未执行/拒绝/延迟、超额告警、
反馈→账户联动 (独立期望值)、重复反馈幂等、目标替代/过期、非系统交易.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from data_pipeline.accounting import AccountError, AccountEvent, AccountService
from data_pipeline.duckdb_manager import DuckDBManager
from orchestration.proposal_ledger import (
    Feedback,
    Proposal,
    ProposalLedger,
    make_proposal_id,
)


def dt(y: int, m: int, d: int, hh: int = 10, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm)


@pytest.fixture()
def env(tmp_path: Path):
    db = DuckDBManager(str(tmp_path / "ledger.duckdb"))
    db.init_database()
    accounts = AccountService(db)
    accounts.ensure_account("ACC-1", "测试账户", mode="real")
    accounts.import_events([
        AccountEvent(
            event_id="init", account_id="ACC-1", event_type="initial",
            event_time=dt(2026, 9, 1), amount=100000.0,
        ),
    ])
    ledger = ProposalLedger(db)
    yield db, accounts, ledger
    db.close()


def buy_proposal(
    shares: float = 1000.0,
    price: float = 4.0,
    pid: str = "P1",
    target_id: str = "T-1",
) -> Proposal:
    return Proposal(
        proposal_id=pid, target_id=target_id, account_id="ACC-1",
        symbol="510300", side="BUY", shares=shares, ref_price=price,
        ref_time=dt(2026, 9, 2, 9, 0), status="proposed",
        valid_until=dt(2026, 9, 5),
    )


# ------------------------------------------------------------------
# 建议创建
# ------------------------------------------------------------------

class TestProposalCreate:
    def test_create_and_dedup(self, env):
        db, _accounts, ledger = env
        r1 = ledger.create_proposals([buy_proposal()])
        r2 = ledger.create_proposals([buy_proposal()])
        assert r1 == (1, 0)
        assert r2 == (0, 1)
        assert db.get_table_count("trade_proposals") == 1

    def test_invalid_side_rejected(self, env):
        _db, _accounts, ledger = env
        bad = buy_proposal()
        bad.side = "HOLD"
        with pytest.raises(AccountError, match="BUY/SELL"):
            ledger.create_proposals([bad])


# ------------------------------------------------------------------
# 反馈流: 部分成交 / 未执行 / 拒绝 / 超额 / 幂等 / 联动
# ------------------------------------------------------------------

class TestFeedbackFlow:
    def test_partial_then_filled_updates_status_and_ledger(self, env):
        db, accounts, ledger = env
        ledger.create_proposals([buy_proposal(shares=1000, price=4.0)])

        r1 = ledger.record_feedback(Feedback(
            feedback_id="FB1", proposal_id="P1", account_id="ACC-1",
            feedback_time=dt(2026, 9, 2, 9, 35), status="partial",
            shares=600, price=4.0, fee=1.2, trade_id="TR1",
        ))
        assert r1.inserted and r1.proposal_status == "partially_filled"
        assert r1.account_event_id == "FB1:trade"
        state = accounts.current_state("ACC-1")
        assert state.cash == pytest.approx(100000.0 - 600 * 4.0 - 1.2)
        assert state.shares_of("510300") == pytest.approx(600)

        r2 = ledger.record_feedback(Feedback(
            feedback_id="FB2", proposal_id="P1", account_id="ACC-1",
            feedback_time=dt(2026, 9, 3, 9, 35), status="filled",
            shares=400, price=4.05, fee=0.8, trade_id="TR2",
        ))
        assert r2.proposal_status == "filled"
        state = accounts.current_state("ACC-1")
        assert state.cash == pytest.approx(100000.0 - 2401.2 - 1620.8)
        assert state.shares_of("510300") == pytest.approx(1000)
        assert db.get_proposal("P1")["status"] == "filled"

    def test_duplicate_feedback_no_double_entry(self, env):
        db, accounts, ledger = env
        ledger.create_proposals([buy_proposal()])
        fb = Feedback(
            feedback_id="FB1", proposal_id="P1", account_id="ACC-1",
            feedback_time=dt(2026, 9, 2, 9, 35), status="filled",
            shares=1000, price=4.0, fee=2.0,
        )
        r1 = ledger.record_feedback(fb)
        r2 = ledger.record_feedback(fb)
        assert r1.inserted is True
        assert r2.inserted is False
        assert db.get_table_count("execution_feedback") == 1
        assert db.get_table_count("account_events") == 2  # initial + 1 笔成交
        assert accounts.current_state("ACC-1").shares_of("510300") == 1000

    def test_not_executed_keeps_proposed_and_no_ledger(self, env):
        db, _accounts, ledger = env
        ledger.create_proposals([buy_proposal()])
        r = ledger.record_feedback(Feedback(
            feedback_id="FB1", proposal_id="P1", account_id="ACC-1",
            feedback_time=dt(2026, 9, 2, 15, 0), status="not_executed",
            reason="未到委托价",
        ))
        assert r.inserted and r.account_event_id is None
        assert db.get_proposal("P1")["status"] == "proposed"
        assert db.get_table_count("account_events") == 1  # 仅 initial

    def test_delayed_keeps_proposed(self, env):
        db, _accounts, ledger = env
        ledger.create_proposals([buy_proposal()])
        ledger.record_feedback(Feedback(
            feedback_id="FB1", proposal_id="P1", account_id="ACC-1",
            feedback_time=dt(2026, 9, 2, 15, 0), status="delayed",
            reason="资金未到位",
        ))
        assert db.get_proposal("P1")["status"] == "proposed"

    def test_rejected_sets_status(self, env):
        db, _accounts, ledger = env
        ledger.create_proposals([buy_proposal()])
        ledger.record_feedback(Feedback(
            feedback_id="FB1", proposal_id="P1", account_id="ACC-1",
            feedback_time=dt(2026, 9, 2, 15, 0), status="rejected",
            reason="手动放弃",
        ))
        assert db.get_proposal("P1")["status"] == "rejected"

    def test_overfill_warning_and_actual_kept(self, env):
        _db, accounts, ledger = env
        ledger.create_proposals([buy_proposal(shares=1000)])
        r = ledger.record_feedback(Feedback(
            feedback_id="FB1", proposal_id="P1", account_id="ACC-1",
            feedback_time=dt(2026, 9, 2, 9, 35), status="filled",
            shares=1200, price=4.0, fee=0.0,
        ))
        assert r.proposal_status == "filled"
        assert any("超额成交" in w for w in r.warnings)
        # 保留真实事实, 不截成建议量
        assert accounts.current_state("ACC-1").shares_of("510300") == 1200

    def test_feedback_ledger_failure_keeps_record(self, env):
        """账本无法入账 (卖超) → 反馈保留 + 告警, 事实不丢."""
        db, _accounts, ledger = env
        sell = Proposal(
            proposal_id="P2", target_id="T-1", account_id="ACC-1",
            symbol="510300", side="SELL", shares=100.0, status="proposed",
        )
        ledger.create_proposals([sell])
        r = ledger.record_feedback(Feedback(
            feedback_id="FB1", proposal_id="P2", account_id="ACC-1",
            feedback_time=dt(2026, 9, 2, 9, 35), status="filled",
            shares=100, price=4.0, fee=0.0,
        ))
        assert r.inserted is True
        assert r.account_event_id is None
        assert any("账本未入账" in w for w in r.warnings)
        assert db.get_table_count("execution_feedback") == 1
        assert db.get_table_count("account_events") == 1  # 仅 initial

    def test_apply_to_ledger_false_skips_booking(self, env):
        """成交已由 account-import 直接入账时 → 反馈仅标注, 不重复计账."""
        db, accounts, ledger = env
        ledger.create_proposals([buy_proposal()])
        accounts.import_events([AccountEvent(
            event_id="manual-1", account_id="ACC-1", event_type="trade",
            event_time=dt(2026, 9, 2, 10, 0), symbol="510300", side="BUY",
            shares=1000, price=4.0, fee=2.0, source="manual",
        )])
        r = ledger.record_feedback(Feedback(
            feedback_id="FB1", proposal_id="P1", account_id="ACC-1",
            feedback_time=dt(2026, 9, 2, 9, 35), status="filled",
            shares=1000, price=4.0, fee=2.0,
        ), apply_to_ledger=False)
        assert r.account_event_id is None
        assert db.get_proposal("P1")["status"] == "filled"
        assert accounts.current_state("ACC-1").shares_of("510300") == 1000
        assert db.get_table_count("account_events") == 2  # initial + manual-1

    def test_non_system_trade_feedback_books(self, env):
        _db, accounts, ledger = env
        r = ledger.record_feedback(Feedback(
            feedback_id="FBX", proposal_id=None, account_id="ACC-1",
            feedback_time=dt(2026, 9, 2, 9, 35), status="filled",
            shares=500, price=4.0, fee=0.0, symbol="510050", side="BUY",
        ))
        assert r.account_event_id == "FBX:trade"
        assert accounts.current_state("ACC-1").shares_of("510050") == 500


# ------------------------------------------------------------------
# 目标替代 / 过期 / 工具函数
# ------------------------------------------------------------------

class TestReplacementAndExpiry:
    def test_mark_target_replaced(self, env):
        db, _accounts, ledger = env
        ledger.create_proposals([
            buy_proposal(pid="P1", target_id="T-1"),
            buy_proposal(pid="P2", target_id="T-2"),
        ])
        n = ledger.mark_target_replaced("T-2")
        assert n == 1
        assert db.get_proposal("P1")["status"] == "replaced"
        assert db.get_proposal("P2")["status"] == "proposed"

    def test_expire_open_proposals(self, env):
        db, _accounts, ledger = env
        ledger.create_proposals([buy_proposal()])   # valid_until 9/5
        n = ledger.expire_open_proposals(dt(2026, 9, 6, 10, 0))
        assert n == 1
        assert db.get_proposal("P1")["status"] == "expired"
        assert db.get_open_proposals("ACC-1").is_empty()

    def test_make_proposal_id_deterministic(self):
        assert make_proposal_id("T-1", "510300", "BUY") == \
            make_proposal_id("T-1", "510300", "BUY")
        assert make_proposal_id("T-1", "510300", "BUY") != \
            make_proposal_id("T-1", "510300", "SELL")
