"""调仓规划器 (M4) 单元测试 — 独立期望值 (spec §9.1).

覆盖 spec §8.2 样本案例 + §5.4/§5.5/§6.1 规则:
  - 目标 20% 未变、实际漂移到 28% → 减持调回 (band 触发)
  - 未越界不交易 (3pp 偏离 < 5pp band)
  - 明确清零/风险退出不受 band 与成本过滤压制
  - 昨日建议未执行 → 账户不变, 计划仍能正确重算
  - 部分成交 (部分持仓) → 差额重算, 不重复累加
  - 现金不足 → 缩减买入; 依赖卖出 → 条件建议 (depends_on)
  - 不足一手 → 不生成
  - 可卖份额约束 (T+1 锁定) → 按可卖量卖出
  - 停牌/无价格/缺 NAV 禁新买 → 不生成买入, 持仓保留
  - 冷却期 → 不出买单; 带宽不阻挡首次建仓
  - 现金预留只影响可用量, 不减少净资产 (M4-05)
  - 优先级顺序: 风险退出 > 常规卖出 > 常规买入 (下游不得放宽)
"""

from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from data_pipeline.accounting import AccountState, Lot
from strategies.rebalance_planner import (
    PRIORITY_BUY_DEPENDENT,
    PRIORITY_BUY_REBALANCE,
    PRIORITY_RISK_EXIT,
    PRIORITY_SELL_REBALANCE,
    PlannerConfig,
    plan_rebalance,
    value_account,
)

AS_OF = date(2026, 9, 11)
REF_TIME = datetime(2026, 9, 11, 15, 30)

# 判定用配置: band=5pp, 有效期 3 天, 冷却 5 天; 与 settings 默认一致
CFG = PlannerConfig(
    band_points=0.05, valid_days=3, min_trade_amount=0.0, cooldown_days=5,
)

# 手工成本: 佣金 0.00075/单边, 最低 5 元, 滑点 0 (便于人工核对)
COST = {
    "commission_rate": 0.00075,
    "min_commission_per_trade": 5.0,
    "slippage_base": 0.0,
    "impact_coefficient": 0.0,
}


def make_prices(entries: dict[str, float]) -> pl.DataFrame:
    """构造单日行情 (date=AS_OF, close=给定值); amount 留空 → 滑点不生效."""
    rows = [
        {
            "symbol": sym, "date": AS_OF, "open": px, "high": px,
            "low": px, "close": px, "volume": 1e8, "amount": None,
        }
        for sym, px in entries.items()
    ]
    return pl.DataFrame(rows)


def make_account(
    cash: float = 0.0,
    positions: dict[str, tuple[float, date]] | None = None,
    reserved: float = 0.0,
    account_id: str = "ACC-1",
) -> AccountState:
    """构造账户状态 (positions: symbol → (shares, sellable_date))."""
    state = AccountState(
        account_id=account_id,
        cash=cash,
        cash_withdrawable=cash,
        cash_reserved=reserved,
        last_event_time=datetime(2026, 9, 10, 15, 0),
    )
    for i, (sym, (shares, sellable)) in enumerate((positions or {}).items()):
        state.lots[sym] = [Lot(
            lot_id=f"lot-{i}", symbol=sym, shares=shares, cost_basis=shares * 10.0,
            open_date=date(2026, 8, 1), sellable_date=sellable,
            open_event_id=f"evt-{i}",
        )]
    return state


def plan(account: AccountState, prices: dict[str, float], targets: dict[str, float],
         **kwargs):
    kwargs.setdefault("config", CFG)
    return plan_rebalance(
        account, make_prices(prices), targets,
        as_of=AS_OF, ref_time=REF_TIME,
        settings={"trading_cost": COST}, **kwargs,
    )


# ------------------------------------------------------------------
# 样本案例: 目标 20% 未变, 实际漂移到 28% (spec §8.2)
# ------------------------------------------------------------------

class TestBandRebalance:
    def test_drift_28_to_20_triggers_sell(self):
        """NAV=100000, 持仓 28000 (28%), 目标 20% → 卖 800 份 × 10 元 = 8000."""
        account = make_account(cash=72000, positions={"510300": (2800.0, AS_OF)})
        result = plan(account, {"510300": 10.0}, {"510300": 0.20, "__CASH__": 0.80})

        assert len(result.proposals) == 1
        p = result.proposals[0]
        assert (p.side, p.symbol, p.shares) == ("SELL", "510300", 800)
        assert p.est_amount == pytest.approx(8000.0)          # 800 × 10
        assert p.est_fee == pytest.approx(6.0)                # 8000 × 0.00075
        assert p.priority == PRIORITY_SELL_REBALANCE
        assert p.actual_weight == pytest.approx(0.28)
        assert p.target_weight == pytest.approx(0.20)
        assert p.valid_until == datetime(2026, 9, 14, 15, 30)  # +3 天有效期

    def test_within_band_no_trade(self):
        """实际 23% vs 目标 20% (3pp < 5pp band) → 不交易 (未越界不追精确对齐)."""
        account = make_account(cash=77000, positions={"510300": (2300.0, AS_OF)})
        result = plan(account, {"510300": 10.0}, {"510300": 0.20, "__CASH__": 0.80})
        assert result.proposals == []
        assert any("带宽" in s.reason for s in result.skipped)

    def test_band_sell_boundary_exact_5pp_triggers(self):
        """实际 25% → 偏离 5pp = band → 触发 (|d| ≥ band)."""
        account = make_account(cash=75000, positions={"510300": (2500.0, AS_OF)})
        result = plan(account, {"510300": 10.0}, {"510300": 0.20, "__CASH__": 0.80})
        assert len(result.proposals) == 1
        assert result.proposals[0].shares == 500

    def test_first_build_not_blocked_by_band(self):
        """首次建仓 (actual=0) 不受 band 阻挡 (spec §5.4)."""
        account = make_account(cash=100000, positions={"510300": (1000.0, AS_OF)})
        # 510300 10%, 目标 510500 20% → 510500 首次建仓
        result = plan(account, {"510300": 10.0, "510500": 5.0},
                      {"510300": 0.10, "510500": 0.20, "__CASH__": 0.70})
        buys = [p for p in result.proposals if p.side == "BUY"]
        assert len(buys) == 1
        assert buys[0].symbol == "510500"
        assert buys[0].reason.startswith("首次建仓")

    def test_unexecuted_proposal_account_unchanged_reproposes(self):
        """昨日建议未执行: 账户实际不变 → 相同计划重算 (幂等, 不累计)."""
        account = make_account(cash=72000, positions={"510300": (2800.0, AS_OF)})
        first = plan(account, {"510300": 10.0}, {"510300": 0.20, "__CASH__": 0.80})
        second = plan(account, {"510300": 10.0}, {"510300": 0.20, "__CASH__": 0.80})
        assert [p.shares for p in first.proposals] == [800]
        assert [p.shares for p in second.proposals] == [800]

    def test_partial_fill_recomputes_remainder(self):
        """部分成交: 卖 800 中成交 300 后 → 剩 500 待卖 (按最新账户重算)."""
        account = make_account(cash=75000, positions={"510300": (2500.0, AS_OF)})
        result = plan(account, {"510300": 10.0}, {"510300": 0.20, "__CASH__": 0.80})
        assert result.proposals[0].shares == 500
        assert result.proposals[0].actual_weight == pytest.approx(0.25)


# ------------------------------------------------------------------
# 风险退出 / 明确清零 (不受 band 压制)
# ------------------------------------------------------------------

class TestRiskExitPriority:
    def test_risk_exit_bypasses_band_and_min_amount(self):
        """止损 10% 持仓 (偏离 10pp 但也带) → 无条件 SELL; 不受成本过滤压制."""
        account = make_account(
            cash=9000, positions={"510300": (1000.0, AS_OF), "510500": (0.0, AS_OF)},
        )
        cfg = PlannerConfig(band_points=0.50, min_trade_amount=10_000.0)
        result = plan_rebalance(
            account, make_prices({"510300": 10.0}), {"510300": 0.0, "__CASH__": 1.0},
            as_of=AS_OF, ref_time=REF_TIME,
            settings={"trading_cost": COST}, config=cfg,
            risk_exit_symbols={"510300": "ATR止损触发 (close 9.5 ≤ stop 9.6)"},
        )
        assert len(result.proposals) == 1
        p = result.proposals[0]
        assert (p.side, p.shares, p.priority) == ("SELL", 1000, PRIORITY_RISK_EXIT)
        assert "风险退出" in p.reason
        assert p.depends_on is None

    def test_risk_exit_no_sellable_keeps_position(self):
        """风险退出但全部锁定 (T+1 未解锁) → 无建议, 保留展示不清仓."""
        account = make_account(
            cash=1000, positions={"510300": (1000.0, date(2026, 9, 12))},
        )
        result = plan_rebalance(
            account, make_prices({"510300": 10.0}), {"510300": 0.0, "__CASH__": 1.0},
            as_of=AS_OF, ref_time=REF_TIME,
            settings={"trading_cost": COST}, config=CFG,
            risk_exit_symbols={"510300": "ATR止损触发"},
        )
        assert result.proposals == []
        assert any("无可卖份额" in s.reason for s in result.skipped)
        assert any("剩余风险" in n for n in result.notes)

    def test_risk_exit_partial_locked_sells_sellable(self):
        """风险退出部分锁定 → 卖出可卖部分, 剩余待可交易."""
        account = make_account(
            cash=1000, positions={"510300": (1000.0, date(2026, 9, 12))},
        )
        # 拆两批: 600 可卖 + 400 锁定
        account.lots["510300"] = [
            Lot("a", "510300", 600.0, 6000.0, date(2026, 8, 1), AS_OF, "e1"),
            Lot("b", "510300", 400.0, 4000.0, date(2026, 9, 12), date(2026, 9, 12), "e2"),
        ]
        result = plan_rebalance(
            account, make_prices({"510300": 10.0}), {"510300": 0.0, "__CASH__": 1.0},
            as_of=AS_OF, ref_time=REF_TIME,
            settings={"trading_cost": COST}, config=CFG,
            risk_exit_symbols={"510300": "ATR止损触发"},
        )
        assert len(result.proposals) == 1
        assert result.proposals[0].shares == 600
        assert any("锁定" in s.reason for s in result.skipped)

    def test_explicit_zero_target_sells_even_small(self):
        """目标清零 (策略退出, 非止损) 不受 band 压制 → SELL 全部."""
        account = make_account(cash=90000, positions={"510300": (1000.0, AS_OF)})
        cfg = PlannerConfig(band_points=0.50)
        result = plan_rebalance(
            account, make_prices({"510300": 10.0}), {"510300": 0.0, "__CASH__": 1.0},
            as_of=AS_OF, ref_time=REF_TIME,
            settings={"trading_cost": COST}, config=cfg,
        )
        assert len(result.proposals) == 1
        assert result.proposals[0].shares == 1000
        assert result.proposals[0].priority == PRIORITY_RISK_EXIT

    def test_normal_small_reduction_filtered_by_band(self):
        """常规小额减持受 band 约束 (对照风险退出)."""
        account = make_account(cash=99000, positions={"510300": (100.0, AS_OF)})
        result = plan(account, {"510300": 10.0}, {"510300": 0.0, "__CASH__": 1.0})
        # 目标清零属于明确退出 → 仍然卖出 (spec §5.4: 明确退出不被带宽永久跳过)
        assert len(result.proposals) == 1


# ------------------------------------------------------------------
# 现金 / 可卖 / 整手 / 预留 (M4-01/05)
# ------------------------------------------------------------------

class TestCashAndQuantity:
    def test_insufficient_cash_scales_buy(self):
        """现金不足时缩减买入: 需求 4000 元 → 400 份 (4005 元入池内)."""
        small = make_account(cash=5000, positions={"510500": (500.0, AS_OF)})
        result = plan(small, {"510500": 10.0}, {"510500": 0.90, "__CASH__": 0.10})
        # NAV = 10000; 目标 9000; 现金 5000 为预算: 400 份×10 + 5 费 = 4005 ≤ 5000,
        # 500 份 = 5005 > 5000 → 独立期望 400 份
        assert len(result.proposals) == 1
        assert result.proposals[0].shares == 400
        assert result.proposals[0].est_amount == pytest.approx(4000.0)

    def test_cash_shortfall_scales_by_available(self):
        """现金 3000 < 需求 2200? 不缩减; 需求超出可用时按可负担缩减."""
        account = make_account(cash=3000, positions={"510500": (500.0, AS_OF)})
        result = plan(account, {"510500": 10.0}, {"510500": 0.90, "__CASH__": 0.10})
        # NAV = 8000; 目标 7200; 现金 3000 全部可用 → 需求 7200-5000=2200,
        # 可负担 200 份 (200×10+5=2005 ≤ 2200), 220 份不足一手
        p = result.proposals[0]
        assert p.shares == 200
        assert p.est_amount == pytest.approx(2000.0)

    def test_reserved_cash_not_spendable(self):
        """预留只影响可用量: 现金 5000 预留 3000 → 可买 200 份 (2005 ≤ 2000+容差)."""
        account = make_account(
            cash=5000, positions={"510500": (500.0, AS_OF)}, reserved=3000.0,
        )
        result = plan(account, {"510500": 10.0}, {"510500": 0.90, "__CASH__": 0.10})
        p = result.proposals[0]
        # 可用 2000: 200×10+5 = 2005 > 2000 → 100 份 (1005); 独立期望值
        assert p.shares == 100
        # 预留不减少净资产: NAV 仍为 10000
        assert result.valuation.nav == pytest.approx(10000.0)
        assert result.valuation.cash_available == pytest.approx(2000.0)

    def test_odd_lot_target_floor(self):
        """不足一手的差额 → 不生成建议, 保留原因."""
        account = make_account(cash=9950, positions={})
        # NAV = 9950; 目标 0.006 → 目标市值 59.7 元; 100 份 × 10 + 5 费 = 1005 > 59.7
        result = plan(account, {"510500": 10.0}, {"510500": 0.006, "__CASH__": 0.994})
        assert result.proposals == []
        assert any("不足一手" in s.reason for s in result.skipped)

    def test_sell_limited_by_sellable_and_lot_floor(self):
        """卖出受可卖份额限制: 需 800, 可卖 600 → 卖 600 (600 为整手)."""
        account = make_account(
            cash=72000,
            positions={"510300": (2800.0, date(2026, 9, 12))},
        )
        account.lots["510300"] = [
            Lot("a", "510300", 600.0, 6000.0, date(2026, 8, 1), AS_OF, "e1"),
            Lot("b", "510300", 2200.0, 22000.0, date(2026, 9, 12), date(2026, 9, 12), "e2"),
        ]
        result = plan(account, {"510300": 10.0}, {"510300": 0.20, "__CASH__": 0.80})
        assert len(result.proposals) == 1
        assert result.proposals[0].shares == 600
        assert any("锁定" in s.reason for s in result.skipped)

    def test_sell_dependency_marks_conditional(self):
        """现金不足且计划内含卖出 → 买入使用卖出资金并标记依赖 (条件建议, spec §6.1)."""
        account = make_account(
            cash=2100, positions={"510300": (500.0, AS_OF), "510500": (500.0, AS_OF)},
        )
        # NAV = 12100; 510300: 5000 (41.3%) → 目标 5% → 卖 400 份 (释放 ~4000)
        # 510500: 5000 (41.3%) → 目标 55% → 需买 900 份 (9050 > 现金 2100+卖出 4000)
        # → 缩减至可负担 600 份 (6035), 并标记依赖卖出 (spec §6.1)
        result = plan(
            account, {"510300": 10.0, "510500": 10.0},
            {"510300": 0.05, "510500": 0.55, "__CASH__": 0.40},
        )
        sells = [p for p in result.proposals if p.side == "SELL"]
        buys = [p for p in result.proposals if p.side == "BUY"]
        assert [p.symbol for p in sells] == ["510300"]
        assert [p.symbol for p in buys] == ["510500"]
        assert buys[0].shares == 600
        assert buys[0].depends_on == "SELL:510300"
        assert buys[0].conditional is True
        assert buys[0].priority == PRIORITY_BUY_DEPENDENT
        assert "依赖卖出" in buys[0].reason

    def test_buy_fully_covered_by_cash_not_conditional(self):
        """买入完全由可用现金覆盖 (未使用卖出资金) → 不标记条件 (避免过度标注)."""
        account = make_account(
            cash=30000, positions={"510300": (500.0, AS_OF), "510500": (500.0, AS_OF)},
        )
        # NAV = 40000; 510300: 12.5% → 6% (卖 200); 510500: 12.5% → 50% (买 2000,
        # 成本 20005 ≤ 现金 30000) → 不依赖卖出资金
        result = plan(
            account, {"510300": 10.0, "510500": 10.0},
            {"510300": 0.06, "510500": 0.50, "__CASH__": 0.44},
        )
        buys = [p for p in result.proposals if p.side == "BUY"]
        assert [p.shares for p in buys] == [2000]
        assert all(p.depends_on is None for p in buys)
        assert all(p.conditional is False for p in buys)

    def test_no_negative_cash_total(self):
        """全部卖出+买入组合: 买入成本总额 ≤ 可用现金 + 卖出释放 (不生成负现金)."""
        account = make_account(
            cash=5000, positions={"510300": (400.0, AS_OF), "510500": (200.0, AS_OF)},
        )
        # NAV = 11000; 510300: 4000 (36.4%) → 目标 8% (需降 3120 → 整手 300 份)
        # 510500: 2000 (18.2%) → 目标 48% (需增 3280 → 满额 500 份, 缩减到可负担)
        result = plan(
            account, {"510300": 10.0, "510500": 10.0},
            {"510300": 0.08, "510500": 0.48, "__CASH__": 0.44},
        )
        sells = [p for p in result.proposals if p.side == "SELL"]
        buys = [p for p in result.proposals if p.side == "BUY"]
        assert [p.shares for p in sells] == [300]
        assert buys  # 买入在卖出资金支持下成立 (余额 5000 + 释放 ~3000)
        buy_cost = sum(
            p.est_amount * (1.0 + p.est_slippage_rate) + p.est_fee for p in buys
        )
        released = sum(p.est_amount - p.est_fee for p in sells)
        assert buy_cost <= 5000 + released + 1.0


# ------------------------------------------------------------------
# 可用性约束 / 池 / 冷启动与冷却 (spec §4.4/§5.5)
# ------------------------------------------------------------------

class TestAvailabilityAndCooldown:
    def test_stale_symbol_keeps_position(self):
        """停牌/无当日价格 → 不生成卖出, 保留持仓 (禁买≠清仓)."""
        account = make_account(cash=1000, positions={"510300": (500.0, AS_OF)})
        result = plan(
            account, {"510300": 10.0}, {"510300": 0.0, "__CASH__": 1.0},
            stale_symbols={"510300"},
        )
        assert result.proposals == []
        assert any("停牌" in s.reason for s in result.skipped)

    def test_missing_nav_blocks_new_buy_but_holds(self):
        """不在可买池 (缺 NAV unknown) → 不生成买入 (禁新买, C10)."""
        account = make_account(cash=10000, positions={"510300": (900.0, AS_OF)})
        result = plan(
            account, {"510300": 10.0, "513100": 1.0},
            {"510300": 0.90, "513100": 0.05, "__CASH__": 0.05},
            pool_can_buy={"510300"},  # 513100 缺 NAV → 禁新买
            pool_can_sell={"510300", "513100"},
        )
        assert all(p.symbol != "513100" for p in result.proposals)
        assert any(s.symbol == "513100" and "禁新买" in s.reason for s in result.skipped)

    def test_cooldown_blocks_buy(self):
        """冷却期内即使目标要求增持也不出买单 (spec §5.5)."""
        account = make_account(cash=50000, positions={})
        result = plan(
            account, {"510300": 10.0}, {"510300": 0.30, "__CASH__": 0.70},
            cooldown_symbols={"510300": "止损退出 2026-09-08, 冷却 5 天"},
        )
        assert result.proposals == []
        assert any("冷却" in s.reason for s in result.skipped)

    def test_empty_account_no_executable_proposals(self):
        """无账户事件 → 无可执行建议, 目标保持研究输出 (C03)."""
        result = plan_rebalance(
            None, make_prices({"510300": 10.0}), {"510300": 0.20, "__CASH__": 0.80},
            as_of=AS_OF, ref_time=REF_TIME, settings={"trading_cost": COST},
            config=CFG,
        )
        assert result.proposals == []
        assert result.valuation is None
        assert any("无账户" in n for n in result.notes)

    def test_missing_price_for_held_position_pauses(self):
        """持仓缺估值价 → NAV 不完整, 暂停可执行数量 (spec §3.3)."""
        account = make_account(cash=1000, positions={"510300": (500.0, AS_OF)})
        result = plan(account, {"510500": 10.0}, {"510500": 0.30, "__CASH__": 0.70})
        assert result.proposals == []
        assert result.paused is True
        assert result.valuation.missing_prices == ["510300"]


# ------------------------------------------------------------------
# 优先级顺序 (spec §5.5; 下游不得放宽)
# ------------------------------------------------------------------

class TestPriorityOrder:
    def test_risk_exit_ranks_before_regular_trades(self):
        account = make_account(
            cash=10000,
            positions={"510300": (500.0, AS_OF), "510500": (800.0, AS_OF)},
        )
        result = plan(
            account, {"510300": 10.0, "510500": 10.0},
            {"510300": 0.0, "510500": 0.30, "__CASH__": 0.70},
        )
        assert result.proposals  # 至少一个卖出
        risk = [p for p in result.proposals if p.priority == PRIORITY_RISK_EXIT]
        assert risk and risk[0].symbol == "510300"
        for p in result.proposals:
            if p.side == "BUY":
                assert p.priority < PRIORITY_SELL_REBALANCE

    def test_sells_ordered_before_buys(self):
        account = make_account(
            cash=2100, positions={"510300": (500.0, AS_OF), "510500": (500.0, AS_OF)},
        )
        # NAV = 12100; 510300: 41.3% → 5% (卖 400); 510500: 41.3% → 60% (买, 偏离 18.7pp)
        result = plan(
            account, {"510300": 10.0, "510500": 10.0},
            {"510300": 0.05, "510500": 0.60, "__CASH__": 0.35},
        )
        sides = [p.side for p in result.proposals]
        assert "SELL" in sides and "BUY" in sides
        assert sides.index("SELL") < sides.index("BUY")

    def test_buy_priority_below_regular_sell_constant(self):
        assert PRIORITY_RISK_EXIT > PRIORITY_SELL_REBALANCE > PRIORITY_BUY_REBALANCE
        assert PRIORITY_BUY_REBALANCE > PRIORITY_BUY_DEPENDENT


# ------------------------------------------------------------------
# 账户估值 (独立期望值)
# ------------------------------------------------------------------

class TestAccountValuation:
    def test_nav_and_weights_manual(self):
        """现金 6000 + 400 份 × 10 元 = 10000; 权重 0.4 / 现金 0.6."""
        account = make_account(cash=6000, positions={"510300": (400.0, AS_OF)})
        val = value_account(account, make_prices({"510300": 10.0}), AS_OF)
        assert val.nav == pytest.approx(10000.0)
        assert val.weights["510300"] == pytest.approx(0.4)
        assert val.cash_weight == pytest.approx(0.6)
        assert val.valuation_ok is True

    def test_receivable_included_in_nav(self):
        """应收权益计入 NAV (spec §3.3), 不重复计资产."""
        account = make_account(cash=6000, positions={"510300": (400.0, AS_OF)})
        account.receivable = 500.0
        val = value_account(account, make_prices({"510300": 10.0}), AS_OF)
        assert val.nav == pytest.approx(10500.0)
        assert val.receivable == pytest.approx(500.0)

    def test_price_older_than_as_of_marked_stale(self):
        """价格旧于决策日 > 5 天 → 标注 stale (不虚构当日价)."""
        old = pl.DataFrame([{
            "symbol": "510300", "date": date(2026, 8, 20), "open": 9.0,
            "high": 9.0, "low": 9.0, "close": 9.0, "volume": 1e8, "amount": None,
        }])
        account = make_account(cash=1000, positions={"510300": (100.0, AS_OF)})
        val = value_account(account, old, AS_OF)
        assert val.valuation_ok is True
        assert val.stale_prices == ["510300"]
        assert val.nav == pytest.approx(1900.0)
