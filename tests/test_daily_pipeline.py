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
from orchestration.daily_pipeline import run_daily_pipeline

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
        "data_fetch": {"source": "sina", "default_start_date": "20190101"},
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
        },
        "universe_filter": {
            "min_avg_volume": 1_000_000, "min_list_days": 60,
            "max_premium_pct": 3.0, "liquidity_concentration_warn": 0.005,
        },
        "portfolio": {
            "method": "inverse_volatility", "vol_lookback": 20,
            "max_positions": 5, "min_positions": 1,
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
            "data_fetch", "universe", "macro_timing",
            "universe_filter", "portfolio", "risk_management",
        ]:
            assert section in settings, f"settings.yaml 缺少配置节: {section}"

        # 标的池含基准 510300
        symbols = [
            etf["symbol"]
            for category in settings["universe"].values()
            if isinstance(category, list)
            for etf in category
        ]
        assert "510300" in symbols
