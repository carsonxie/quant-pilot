"""每日主流程编排 (T0.7) 集成测试.

覆盖:
  - 全流程跑通 (合成数据 + FakeFetcher + 临时 DuckDB)
  - 幂等性: 重复运行不产生重复行, 信号内容一致
  - 数据护栏: 脏数据 (负价格) 阻断流程, 不写信号
  - 无未来函数: 未来数据"曾存在后被移除"与"从未存在"产出一致
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest
import yaml

from data_pipeline.duckdb_manager import DuckDBManager
from data_pipeline.processors.validator import DataQualityError
from orchestration.daily_pipeline import (
    _step_generate_signals,
    _step_optimize_scoped,
    run_daily_pipeline,
)
from strategies import portfolio_optimizer, risk_manager
from strategies.macro_timing import MacroScore

# ------------------------------------------------------------------
# 合成数据
# ------------------------------------------------------------------

SYMBOLS = ["510300", "510050", "159915"]
N_DAYS = 130  # 业务日数 (需 ≥ 60 上市天数 + MA60 + ATR 窗口)


def _business_days(end: date, n: int) -> list[date]:
    """从 end 向前取 n 个工作日 (周一~周五)."""
    days: list[date] = []
    d = end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


def make_synthetic_ohlcv(symbols: list[str], n_days: int = N_DAYS) -> pl.DataFrame:
    """确定性合成 OHLCV: 缓慢上行 + 小幅正弦波动, 无随机性.

    不同 symbol 有不同波动幅度, 使逆波动率加权结果可区分.
    """
    days = _business_days(date.today(), n_days)
    rows: list[dict] = []
    for s_idx, sym in enumerate(symbols):
        base = 1.0 + s_idx * 0.5
        vol_scale = 0.003 * (s_idx + 1)  # 波动逐级放大
        for i, d in enumerate(days):
            drift = 1.0 + 0.0005 * i
            osc = 1.0 + vol_scale * math.sin(i / 3.0)
            close = base * drift * osc
            rows.append({
                "symbol": sym,
                "date": d,
                "open": close * 0.999,
                "high": close * 1.005,
                "low": close * 0.995,
                "close": close,
                "volume": 2_000_000.0,
                "amount": 8_000_000.0,
            })
    return pl.DataFrame(rows)


class FakeFetcher:
    """测试用数据获取器: 从合成全集按日期区间切片."""

    def __init__(self, full_df: pl.DataFrame) -> None:
        self._full = full_df

    def fetch(self, symbols: list[str], start_date: str, end_date: str) -> pl.DataFrame:
        start = _yyyymmdd_to_date(start_date)
        end = _yyyymmdd_to_date(end_date)
        return self._full.filter(
            pl.col("symbol").is_in(symbols)
            & (pl.col("date") >= start)
            & (pl.col("date") <= end)
        )


def _yyyymmdd_to_date(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def make_settings(symbols: list[str]) -> dict:
    """最小可用 settings (参数与 config/settings.yaml 结构一致)."""
    return {
        "project": {"mode": "research", "strategy_version": "v0.2"},
        "data_fetch": {"source": "sina", "default_start_date": "20190101"},
        "data_quality": {
            "daily_change": {
                "default_limit_pct": 10.0,
                "limit_overrides_pct": {"159915": 20.0, "588000": 20.0},
                "review_buffer_pct": 2.0,
            },
        },
        "universe": {
            "broad_base": [
                {"symbol": s, "name": f"ETF_{s}", "market_type": "A股", "settlement": "T+1"}
                for s in symbols
            ],
        },
        "macro_timing": {
            "ma_short": 20, "ma_long": 60,
            "weight_trend": 0.30, "weight_breadth": 0.25,
            "score_plunge_threshold": 30,
            "scope": "a_share",
        },
        "universe_filter": {
            "min_avg_volume": 1_000_000, "min_list_days": 60,
            "max_premium_pct": 3.0, "liquidity_concentration_warn": 0.005,
        },
        "portfolio": {
            "method": "inverse_volatility", "vol_lookback": 20,
            "max_positions": 5, "min_positions": 1,
            "non_a_share_max_total": 0.30,
        },
        "risk_management": {
            "atr_period": 14, "atr_multiplier": 2.0,
            "safe_position_pct": 0.30, "min_hold_days": 3,
            "turnover_threshold": 0.15,
        },
    }


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "test.duckdb")


@pytest.fixture()
def full_df() -> pl.DataFrame:
    return make_synthetic_ohlcv(SYMBOLS)


@pytest.fixture()
def settings() -> dict:
    return make_settings(SYMBOLS)


def _weights_of(result) -> dict[str, float]:
    return {w.symbol: w.final_weight for w in result.portfolio.weights}


# ------------------------------------------------------------------
# 测试用例
# ------------------------------------------------------------------

class TestFullPipeline:
    """全流程跑通."""

    def test_first_run_produces_signals(self, settings, db_path, full_df):
        fetcher = FakeFetcher(full_df)
        result = run_daily_pipeline(settings, db_path, fetcher=fetcher)

        # 信号日期 = 数据最新交易日
        assert result.run_date == str(full_df["date"].max())

        # 五层漏斗均有产出
        assert result.macro_score is not None
        assert 0.0 <= result.macro_score.composite_score <= 100.0
        assert len(result.passed_symbols) == len(SYMBOLS)
        assert result.portfolio is not None
        assert result.risk_report is not None
        assert len(result.signals) > 0

        # 权重和 ≤ 仓位上限
        total = sum(_weights_of(result).values())
        assert total <= result.macro_score.position_cap + 1e-6

        # 信号已落库
        db = DuckDBManager(db_path)
        n_signals = db.get_table_count("trade_signals")
        assert n_signals == len(result.signals)
        # OHLCV 全量入库
        assert db.get_table_count("ohlcv_daily") == len(full_df)
        db.close()

        # 每步耗时已记录
        assert "macro_timing" in result.step_timings
        assert "signal_persist" in result.step_timings

    def test_summary_is_readable(self, settings, db_path, full_df):
        fetcher = FakeFetcher(full_df)
        result = run_daily_pipeline(settings, db_path, fetcher=fetcher)
        summary = result.summary()
        assert "宏观择时" in summary
        assert "交易信号" in summary
        for sig in result.signals:
            assert sig.trigger_reason  # 每条信号都有归因


class TestIdempotency:
    """幂等性: 重复运行无重复行, 结果一致."""

    def test_rerun_no_duplicates(self, settings, db_path, full_df):
        fetcher = FakeFetcher(full_df)
        r1 = run_daily_pipeline(settings, db_path, fetcher=fetcher)
        r2 = run_daily_pipeline(settings, db_path, fetcher=fetcher)

        db = DuckDBManager(db_path)
        assert db.get_table_count("ohlcv_daily") == len(full_df)
        assert db.get_table_count("trade_signals") == len(r1.signals)
        db.close()

        # 两次运行产出一致 (重跑时 before_date 排除同日信号 → prev 为空)
        assert r2.run_date == r1.run_date
        assert _weights_of(r2) == pytest.approx(_weights_of(r1))
        sig1 = [(s.symbol, s.direction, s.target_weight) for s in r1.signals]
        sig2 = [(s.symbol, s.direction, s.target_weight) for s in r2.signals]
        assert sig1 == sig2


class TestDataQualityGate:
    """数据护栏: 阻断级脏数据终止流程."""

    def test_negative_price_blocks_pipeline(self, settings, db_path, full_df):
        dirty = full_df.with_columns(
            pl.when((pl.col("symbol") == "510300") & (pl.col("date") == full_df["date"].max()))
            .then(-1.0)
            .otherwise(pl.col("close"))
            .alias("close")
        )
        fetcher = FakeFetcher(dirty)

        with pytest.raises(DataQualityError):
            run_daily_pipeline(settings, db_path, fetcher=fetcher)

        # 阻断后不应有任何信号写入
        db = DuckDBManager(db_path)
        assert db.get_table_count("trade_signals") == 0
        db.close()


class TestNoLookahead:
    """无未来函数: T 日结果不受 T 日之后数据影响.

    方法: 先在只含 ≤T 数据的库中跑出结果 B;
    再补拉 T 之后的数据并运行一次 (产生 T_end 信号);
    然后删除 >T 的数据再跑一次得到结果 C.
    若 B == C, 说明 T 日计算未受未来数据污染.
    """

    def test_future_data_does_not_change_past_result(self, settings, db_path, full_df):
        all_dates = sorted(full_df["date"].unique().to_list())
        cutoff = all_dates[-20]  # T = 倒数第 20 个交易日
        t_str = cutoff.strftime("%Y%m%d")

        # --- 第一次: 只跑到 T ---
        fetcher = FakeFetcher(full_df)
        result_b = run_daily_pipeline(settings, db_path, fetcher=fetcher, end_date=t_str)
        assert result_b.run_date == str(cutoff)

        # --- 第二次: 补拉到最新 (库中出现未来数据) ---
        run_daily_pipeline(settings, db_path, fetcher=fetcher)

        # --- 删除 >T 数据, 重跑到 T ---
        db = DuckDBManager(db_path)
        db.conn.execute("DELETE FROM ohlcv_daily WHERE date > ?", [cutoff])
        db.conn.execute("DELETE FROM trade_signals WHERE signal_date > ?", [cutoff])
        db.close()

        result_c = run_daily_pipeline(settings, db_path, fetcher=fetcher, end_date=t_str)

        # --- B 与 C 必须一致 ---
        assert result_c.run_date == result_b.run_date
        assert result_c.macro_score.composite_score == pytest.approx(
            result_b.macro_score.composite_score
        )
        assert _weights_of(result_c) == pytest.approx(_weights_of(result_b))
        sig_b = [(s.symbol, s.direction, s.target_weight) for s in result_b.signals]
        sig_c = [(s.symbol, s.direction, s.target_weight) for s in result_c.signals]
        assert sig_b == sig_c


# ------------------------------------------------------------------
# settings.yaml 结构兼容性 (防止配置漂移)
# ------------------------------------------------------------------

class TestSettingsCompatibility:
    """真实 config/settings.yaml 包含 pipeline 所需的全部配置节."""

    def test_real_settings_has_required_sections(self):
        project_root = Path(__file__).resolve().parents[1]
        with open(project_root / "config" / "settings.yaml", encoding="utf-8") as f:
            settings = yaml.safe_load(f)

        for section in [
            "project", "data_fetch", "universe", "macro_timing",
            "universe_filter", "portfolio", "risk_management",
        ]:
            assert section in settings, f"settings.yaml 缺少配置节: {section}"

        # 研究模式与策略版本标识 (spec §10.1 / A0-1)
        assert settings["project"].get("mode") == "research"
        assert settings["project"].get("strategy_version")
        # 成本情景命名 (spec §6.3 / C08)
        assert settings["trading_cost"].get("scenario") == "legacy_stress"
        # 宏观 scope 与产品感知质检 (spec §5.2 / §4.4, C11/C10)
        assert settings["macro_timing"].get("scope") == "a_share"
        assert "data_quality" in settings

        # 标的池含基准 510300
        symbols = [
            etf["symbol"]
            for category in settings["universe"].values()
            if isinstance(category, list)
            for etf in category
        ]
        assert "510300" in symbols


# ------------------------------------------------------------------
# C04: 防抖不得压制退出/风险 (spec §5.4/§5.5)
# ------------------------------------------------------------------

def _mk_portfolio(weight_map: dict[str, float]) -> portfolio_optimizer.PortfolioResult:
    return portfolio_optimizer.PortfolioResult(
        weights=[
            portfolio_optimizer.PortfolioWeight(
                symbol=s, annualized_vol=0.15, raw_weight=w, final_weight=w,
            )
            for s, w in weight_map.items()
        ],
        position_cap=0.8,
        total_weight=sum(weight_map.values()),
    )


def _mk_risk(forced: list[str] | None = None, plunge: bool = False) -> risk_manager.RiskReport:
    return risk_manager.RiskReport(
        stops=[],
        plunge_detected=plunge,
        plunge_score_drop=40.0 if plunge else 0.0,
        original_position_cap=0.8,
        effective_position_cap=0.3 if plunge else 0.8,
        forced_exits=list(forced or []),
    )


def _mk_score(composite: float = 60.0) -> MacroScore:
    return MacroScore(
        trend_score=composite, breadth_score=composite,
        composite_score=composite, position_cap=0.8,
    )


def _mk_prev(weight_map: dict[str, float]) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": list(weight_map),
        "target_weight": list(weight_map.values()),
    })


class TestSignalDebounce:
    """防抖只约束常规调整; 退出/止损/骤降减仓不受压制."""

    def test_small_exit_not_blocked(self):
        """10% 持仓目标清零 (未入选) → SELL 0%, 不被 15% 防抖挡回."""
        signals = _step_generate_signals(
            _mk_portfolio({}), _mk_risk(), _mk_score(),
            _mk_prev({"A": 0.10}), 0.15,
        )
        sig = {s.symbol: s for s in signals}["A"]
        assert sig.direction == "SELL"
        assert sig.target_weight == pytest.approx(0.0)
        assert "明确退出" in sig.trigger_reason
        assert "防抖" in sig.trigger_reason

    def test_forced_exit_bypasses_debounce(self):
        """止损剔除的小仓位 → SELL, 原因含止损与防抖说明, 且不回补."""
        signals = _step_generate_signals(
            _mk_portfolio({}), _mk_risk(forced=["A"]), _mk_score(),
            _mk_prev({"A": 0.10}), 0.15,
        )
        sig = {s.symbol: s for s in signals}["A"]
        assert sig.direction == "SELL"
        assert sig.target_weight == pytest.approx(0.0)
        assert "ATR止损" in sig.trigger_reason
        assert "不受防抖限制" in sig.trigger_reason
        assert all(s.direction != "BUY" for s in signals)

    def test_plunge_reduce_bypasses_debounce(self):
        """骤降减仓 20% → 18% (2pp < 15pp) → SELL."""
        signals = _step_generate_signals(
            _mk_portfolio({"A": 0.18}), _mk_risk(plunge=True), _mk_score(30.0),
            _mk_prev({"A": 0.20}), 0.15,
        )
        sig = {s.symbol: s for s in signals}["A"]
        assert sig.direction == "SELL"
        assert "风险减仓" in sig.trigger_reason

    def test_normal_small_adjustment_holds(self):
        """常规 20% → 22% (2pp < 15pp) → HOLD 维持 20%."""
        signals = _step_generate_signals(
            _mk_portfolio({"A": 0.22}), _mk_risk(), _mk_score(),
            _mk_prev({"A": 0.20}), 0.15,
        )
        sig = {s.symbol: s for s in signals}["A"]
        assert sig.direction == "HOLD"
        assert sig.target_weight == pytest.approx(0.20)
        assert "防抖阈值" in sig.trigger_reason

    def test_normal_large_change_buys(self):
        """常规 20% → 40% (20pp ≥ 15pp) → BUY."""
        signals = _step_generate_signals(
            _mk_portfolio({"A": 0.40}), _mk_risk(), _mk_score(),
            _mk_prev({"A": 0.20}), 0.15,
        )
        sig = {s.symbol: s for s in signals}["A"]
        assert sig.direction == "BUY"
        assert sig.target_weight == pytest.approx(0.40)

    def test_first_entry_bypasses_debounce(self):
        """首次建仓 10.1% (prev=0) → BUY, 不被 15% 防抖永久跳过 (spec §5.4)."""
        signals = _step_generate_signals(
            _mk_portfolio({"A": 0.101}), _mk_risk(), _mk_score(),
            _mk_prev({}), 0.15,
        )
        sig = {s.symbol: s for s in signals}["A"]
        assert sig.direction == "BUY"
        assert sig.target_weight == pytest.approx(0.101)
        assert "首次建仓" in sig.trigger_reason


class TestRiskExitIntegration:
    """ATR 止损在完整流程中剔除标的且不产生回补 BUY."""

    def test_crash_symbol_forced_out_and_sold(self, settings, db_path):
        """第一轮正常 → 次轮某标的暴跌 15% → 止损剔除, 信号 SELL."""
        full = make_synthetic_ohlcv(SYMBOLS, n_days=131)
        dates = sorted(full["date"].unique().to_list())
        cutoff, crash_day = dates[-2], dates[-1]
        prev_close = float(
            full.filter(
                (pl.col("symbol") == "159915") & (pl.col("date") == cutoff)
            )["close"][0]
        )
        crash = (
            full.with_columns(
                pl.when(
                    (pl.col("symbol") == "159915") & (pl.col("date") == crash_day)
                ).then(pl.lit(prev_close * 0.85))
                .otherwise(pl.col("close")).alias("close"),
            ).with_columns(
                pl.when(
                    (pl.col("symbol") == "159915") & (pl.col("date") == crash_day)
                ).then(pl.lit(prev_close * 0.86))
                .otherwise(pl.col("open")).alias("open"),
            )
        )

        fetcher = FakeFetcher(crash)
        r1 = run_daily_pipeline(
            settings, db_path, fetcher=fetcher, end_date=cutoff.strftime("%Y%m%d"),
        )
        assert "159915" in {s.symbol for s in r1.signals}  # 第一轮有目标

        r2 = run_daily_pipeline(settings, db_path, fetcher=fetcher)
        assert r2.run_date == str(crash_day)
        assert "159915" in r2.risk_report.forced_exits
        assert "159915" not in {w.symbol for w in r2.portfolio.weights}
        sig = {s.symbol: s for s in r2.signals}["159915"]
        assert sig.direction == "SELL"
        assert sig.target_weight == pytest.approx(0.0)
        assert "ATR止损" in sig.trigger_reason


# ------------------------------------------------------------------
# C11: 宏观 scope — A 股信号不隐式清零非 A 资产 (spec §5.2)
# ------------------------------------------------------------------

MIXED_SYMBOLS = ["510300", "510050", "518880"]


def make_mixed_settings() -> dict:
    """2 只 A 股 + 1 只商品 ETF 的 settings."""
    settings = make_settings(MIXED_SYMBOLS)
    settings["universe"] = {
        "broad_base": [
            {"symbol": "510300", "name": "沪深300ETF",
             "market_type": "A股", "settlement": "T+1"},
            {"symbol": "510050", "name": "上证50ETF",
             "market_type": "A股", "settlement": "T+1"},
        ],
        "cross_border_commodity": [
            {"symbol": "518880", "name": "黄金ETF",
             "market_type": "商品", "settlement": "T+0"},
        ],
    }
    return settings


def make_falling_market(n_days: int = 130) -> pl.DataFrame:
    """A 股持续下跌 (宏观得分低) + 黄金平稳微升 (非A)."""
    days = _business_days(date.today(), n_days)
    rows: list[dict] = []
    for i, d in enumerate(days):
        a_price = 2.0 * (0.99 ** i)      # -1%/日 → 趋势/宽度低
        g_price = 3.0 * (1.0003 ** i)    # 平稳微升 → 站上 MA20
        for sym, price in (
            ("510300", a_price),
            ("510050", a_price * 0.98),
            ("518880", g_price),
        ):
            rows.append({
                "symbol": sym, "date": d,
                "open": price * 0.999, "high": price * 1.003,
                "low": price * 0.997, "close": price,
                "volume": 2_000_000.0, "amount": 8_000_000.0,
            })
    return pl.DataFrame(rows)


class TestMacroScope:
    """scope=a_share 只限制 A 股合计; 非A独立上限不被 A 股信号清零."""

    def test_non_a_assets_not_dragged_by_a_share_signal(self, tmp_path):
        settings = make_mixed_settings()
        db_path = str(tmp_path / "scope.duckdb")
        fetcher = FakeFetcher(make_falling_market())

        result = run_daily_pipeline(settings, db_path, fetcher=fetcher)

        assert result.macro_score.position_cap == pytest.approx(0.0, abs=1e-9)
        weights = {w.symbol: w.final_weight for w in result.portfolio.weights}
        # 非A 黄金仍在独立上限内建立目标; A 股被宏观上限压到 0
        assert weights.get("518880") == pytest.approx(0.30, abs=1e-6)
        assert "510300" not in weights and "510050" not in weights
        sig = {s.symbol: s for s in result.signals}
        assert sig["518880"].direction == "BUY"
        assert sig["518880"].target_weight == pytest.approx(0.30)
        assert "510300" not in sig  # 无目标且无历史 → 不产生信号

    def test_all_composite_scope_zeroes_everything(self, tmp_path):
        """显式研究版本: 全组合择时 (v0.1 旧行为) — 宏观 0 → 全部清零."""
        settings = make_mixed_settings()
        settings["macro_timing"]["scope"] = "all_composite"
        db_path = str(tmp_path / "scope2.duckdb")
        fetcher = FakeFetcher(make_falling_market())

        result = run_daily_pipeline(settings, db_path, fetcher=fetcher)

        assert result.macro_score.position_cap == pytest.approx(0.0, abs=1e-9)
        assert result.portfolio.weights == []
        assert all(s.direction != "BUY" for s in result.signals)

    def test_scoped_optimize_respects_bucket_caps(self, settings):
        """分桶优化独立期望值: A 合计 ≤ a_cap; 非A ≤ non_a_cap."""
        opt = portfolio_optimizer.from_settings(settings)
        df = make_falling_market()
        result = _step_optimize_scoped(
            opt, df, MIXED_SYMBOLS,
            a_share_cap=0.6, non_a_cap=0.2,
            market_types={"510300": "A股", "510050": "A股", "518880": "商品"},
            scope="a_share",
        )
        w = {x.symbol: x.final_weight for x in result.weights}
        a_sum = sum(v for k, v in w.items() if k != "518880")
        assert a_sum == pytest.approx(0.6, abs=1e-6)
        assert w["518880"] == pytest.approx(0.2, abs=1e-6)
        assert result.total_weight == pytest.approx(0.8, abs=1e-6)
