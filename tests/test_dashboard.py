"""信号面板数据层 (T0.10) 单元与集成测试.

覆盖:
  - load_latest_signals: 读取最新信号日; 库不存在返回空表且不创建文件
  - build_signal_table / build_position_view: 中文列/百分比/排序/空仓兜底
  - build_price_curve: 归一化 (每标的首日 = 100), 窗口截断
  - load_macro_history: 逐日重算与 MacroTiming 直接计算一致 (防窗口前史 bug)
  - 真实 DuckDB (存在时): 面板信号与库中记录一致 (T0.10 验收项)
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
import yaml

from dashboard.data_loader import (
    build_position_view,
    build_price_curve,
    build_signal_table,
    load_latest_signals,
    load_macro_history,
    load_symbol_names,
)
from data_pipeline.duckdb_manager import DuckDBManager
from strategies.macro_timing import MacroTiming

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "quant_pilot.duckdb"

SYMBOLS = ["510300", "510050", "159920"]
N_DAYS = 130


# ------------------------------------------------------------------
# 合成数据
# ------------------------------------------------------------------

def _business_days(end: date, n: int) -> list[date]:
    days: list[date] = []
    d = end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


def make_ohlcv(symbols: list[str], n_days: int = N_DAYS) -> pl.DataFrame:
    days = _business_days(date.today(), n_days)
    rows = []
    for s_idx, sym in enumerate(symbols):
        base = 1.0 + s_idx * 0.5
        for i, d in enumerate(days):
            close = base * (1.0 + 0.0005 * i) * (1.0 + 0.003 * math.sin(i / 3.0))
            rows.append({
                "symbol": sym, "date": d, "open": close * 0.999,
                "high": close * 1.005, "low": close * 0.995, "close": close,
                "volume": 2_000_000.0, "amount": 8_000_000.0,
            })
    return pl.DataFrame(rows)


def make_settings() -> dict:
    return {
        "universe": {
            "broad_base": [
                {"symbol": "510300", "name": "沪深300ETF",
                 "market_type": "A股", "settlement": "T+1"},
                {"symbol": "510050", "name": "上证50ETF",
                 "market_type": "A股", "settlement": "T+1"},
            ],
            "cross_border_commodity": [
                {"symbol": "159920", "name": "恒生ETF",
                 "market_type": "跨境", "settlement": "T+0"},
            ],
        },
        "macro_timing": {
            "ma_short": 20, "ma_long": 60,
            "weight_trend": 0.30, "weight_breadth": 0.25,
            "score_plunge_threshold": 30,
        },
    }


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture()
def seeded_db(tmp_path: Path) -> str:
    """临时库: 合成 OHLCV + 两个信号日 (最新日 3 条 + 旧日 1 条)."""
    path = str(tmp_path / "dash.duckdb")
    db = DuckDBManager(path)
    db.init_database()
    df = make_ohlcv(SYMBOLS)
    db.upsert_ohlcv(df)

    dates = sorted(df["date"].unique().to_list())
    latest, previous = dates[-1], dates[-2]
    rows = [
        {"signal_id": 9001, "signal_date": previous, "symbol": "510300",
         "direction": "BUY", "target_weight": 0.25, "trigger_reason": "旧信号"},
        {"signal_id": 9002, "signal_date": latest, "symbol": "510050",
         "direction": "BUY", "target_weight": 0.15, "trigger_reason": "测试原因A"},
        {"signal_id": 9003, "signal_date": latest, "symbol": "510300",
         "direction": "HOLD", "target_weight": 0.0, "trigger_reason": "测试原因B"},
        {"signal_id": 9004, "signal_date": latest, "symbol": "159920",
         "direction": "SELL", "target_weight": 0.0, "trigger_reason": "测试原因C"},
    ]
    db.upsert_signals(pl.DataFrame(rows))
    db.close()
    return path


# ------------------------------------------------------------------
# 信号读取
# ------------------------------------------------------------------

class TestLoadSignals:
    def test_returns_latest_signal_day_only(self, seeded_db):
        signals = load_latest_signals(seeded_db)
        assert signals.height == 3           # 旧信号日的 1 条不出现
        by_symbol = dict(
            zip(signals["symbol"], signals["trigger_reason"], strict=True)
        )
        assert by_symbol == {
            "159920": "测试原因C", "510050": "测试原因A", "510300": "测试原因B",
        }

    def test_missing_db_returns_empty_and_does_not_create(self, tmp_path):
        missing = str(tmp_path / "not_exist.duckdb")
        assert load_latest_signals(missing).is_empty()
        assert not Path(missing).exists()    # 只读加载不应创建库文件


# ------------------------------------------------------------------
# 展示表组装
# ------------------------------------------------------------------

class TestViews:
    def test_signal_table_columns_and_format(self, seeded_db):
        settings = make_settings()
        names = load_symbol_names(settings)
        table = build_signal_table(load_latest_signals(seeded_db), names)

        assert table.columns == ["代码", "名称", "方向", "目标仓位", "版本", "触发原因"]
        row = table.filter(pl.col("代码") == "510050").row(0, named=True)
        assert row["名称"] == "上证50ETF"
        assert row["方向"] == "BUY"
        assert row["目标仓位"] == "15.0%"
        assert row["版本"] == "-"  # 旧库无 strategy_version 列时的回退
        assert row["触发原因"] == "测试原因A"

    def test_position_view_only_positive_sorted_desc(self):
        signals = pl.DataFrame({
            "symbol": ["510300", "510050", "159920"],
            "direction": ["BUY", "BUY", "HOLD"],
            "target_weight": [0.10, 0.25, 0.0],
            "trigger_reason": ["a", "b", "c"],
        })
        view = build_position_view(signals, {"510300": "沪深300ETF"})
        assert view.height == 2
        assert view["代码"].to_list() == ["510050", "510300"]   # 仓位降序
        assert view["名称"].to_list() == ["510050", "沪深300ETF"]  # 无映射回退代码
        assert view["目标仓位"].to_list() == ["25.0%", "10.0%"]

    def test_position_view_empty_when_all_zero(self, seeded_db):
        signals = load_latest_signals(seeded_db).with_columns(
            pl.lit(0.0).alias("target_weight")
        )
        assert build_position_view(signals, {}).is_empty()

    def test_position_view_empty_input(self):
        assert build_position_view(pl.DataFrame(), {}).is_empty()


# ------------------------------------------------------------------
# 价格曲线
# ------------------------------------------------------------------

class TestPriceCurve:
    def test_normalized_first_day_is_100(self, seeded_db):
        curve = build_price_curve(seeded_db, ["510300", "510050"], n_days=30)

        assert curve.height == 30
        assert curve.columns[0] == "date"
        assert set(curve.columns[1:]) == {"510300", "510050"}
        first = curve.row(0, named=True)
        assert first["510300"] == pytest.approx(100.0)
        assert first["510050"] == pytest.approx(100.0)
        # 最后一行保留了相对涨幅
        last = curve.row(-1, named=True)
        assert last["510300"] != pytest.approx(100.0)

    def test_missing_db_returns_empty(self, tmp_path):
        missing = str(tmp_path / "nope.duckdb")
        assert build_price_curve(missing, ["510300"]).is_empty()

    def test_empty_symbols_returns_empty(self, seeded_db):
        assert build_price_curve(seeded_db, []).is_empty()


# ------------------------------------------------------------------
# 宏观得分历史 (关键一致性: 重算窗口 vs 直接计算)
# ------------------------------------------------------------------

class TestMacroHistory:
    def _direct_score(self, ohlcv: pl.DataFrame, settings: dict, t: date):
        cfg = settings["macro_timing"]
        macro = MacroTiming(
            ma_short=cfg["ma_short"], ma_long=cfg["ma_long"],
            weight_trend=cfg["weight_trend"], weight_breadth=cfg["weight_breadth"],
            plunge_threshold=cfg["score_plunge_threshold"],
        )
        window = ohlcv.filter(pl.col("date") <= t)
        benchmark = window.filter(pl.col("symbol") == "510300")
        return macro.evaluate(benchmark, window)

    def test_history_matches_direct_evaluation(self, seeded_db):
        settings = make_settings()
        hist = load_macro_history(seeded_db, settings, n_days=30)

        assert hist.height == 30
        assert set(hist.columns) == {
            "date", "composite_score", "position_cap", "regime", "breadth_pct",
        }

        db = DuckDBManager(seeded_db)
        ohlcv = db.get_ohlcv(symbols=SYMBOLS)
        db.close()

        # 首尾两点与"全量数据直接计算"完全一致 (防窗口前史不足 bug)
        for t in (hist["date"][0], hist["date"][-1]):
            direct = self._direct_score(ohlcv, settings, t)
            row = hist.filter(pl.col("date") == t).row(0, named=True)
            assert row["composite_score"] == pytest.approx(direct.composite_score)
            assert row["position_cap"] == pytest.approx(direct.position_cap)
            assert row["breadth_pct"] == pytest.approx(direct.breadth_pct)

    def test_missing_db_returns_empty(self, tmp_path):
        missing = str(tmp_path / "nope.duckdb")
        assert load_macro_history(missing, make_settings()).is_empty()

    def test_step_sampling_includes_latest_date(self, seeded_db):
        hist = load_macro_history(seeded_db, make_settings(), n_days=30, step=5)
        # 30 天取索引 0,5,10,15,20,25 + 末点 → 7 行
        assert hist.height == 7

        db = DuckDBManager(seeded_db)
        ohlcv = db.get_ohlcv(symbols=SYMBOLS)
        db.close()
        assert hist["date"][-1] == ohlcv["date"].max()


# ------------------------------------------------------------------
# 真实数据集成 (T0.10 验收: 信号与 DuckDB 记录一致)
# ------------------------------------------------------------------

requires_db = pytest.mark.skipif(not DB_PATH.exists(), reason="本地 DuckDB 不存在")


@requires_db
class TestRealData:
    def test_signals_match_db_records(self):
        panel_signals = load_latest_signals(str(DB_PATH))

        db = DuckDBManager(str(DB_PATH))
        try:
            db_signals = db.get_latest_signals()
        finally:
            db.close()

        assert panel_signals.height == db_signals.height
        assert panel_signals["symbol"].to_list() == db_signals["symbol"].to_list()
        assert panel_signals["target_weight"].to_list() == \
            db_signals["target_weight"].to_list()

    def test_real_macro_history_runs_to_latest_date(self):
        with open(PROJECT_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
            settings = yaml.safe_load(f)

        hist = load_macro_history(str(DB_PATH), settings, n_days=30)
        assert hist.height == 30

        db = DuckDBManager(str(DB_PATH))
        try:
            ohlcv = db.get_ohlcv(symbols=["510300"])
        finally:
            db.close()
        assert hist["date"][-1] == ohlcv["date"].max()
        assert hist["composite_score"].min() >= 0.0
        assert hist["composite_score"].max() <= 100.0

    def test_real_position_and_curve(self):
        with open(PROJECT_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
            settings = yaml.safe_load(f)
        names = load_symbol_names(settings)
        signals = load_latest_signals(str(DB_PATH))

        positions = build_position_view(signals, names)
        # 空仓或非空皆合法; 非空时曲线可生成
        if not positions.is_empty():
            curve = build_price_curve(
                str(DB_PATH), positions["代码"].to_list(), n_days=60,
            )
            assert curve.height == 60


@requires_db
def test_streamlit_app_renders_without_exception():
    """AppTest 无头执行 dashboard/app.py 全流程, 断言无脚本异常 (T0.10 冒烟)."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(
        str(PROJECT_ROOT / "dashboard" / "app.py"), default_timeout=120,
    )
    at.run()

    assert not at.exception
    # C03/A0-6: 模拟状态声明与数据时间/版本可见
    assert any("研究/模拟模式" in w.value for w in at.warning)
    assert any(
        "数据截止" in c.value and "策略版本" in c.value for c in at.caption
    )
    # M6-04: 账户视图区块存在 (真实库当前无账户 → 显示未配置提示)
    subheaders = " | ".join(s.value for s in at.subheader)
    assert "实际 vs 目标" in subheaders
    assert "待处理建议与反馈" in subheaders
    assert "净值对照" in subheaders
    assert any("未配置账户" in i.value for i in at.info)


# ------------------------------------------------------------------
# M6: 账户视图 (实际/目标 / 对照 / 待处理建议 / 运行状态)
# ------------------------------------------------------------------

@pytest.fixture()
def seeded_account_db(tmp_path: Path) -> str:
    """临时库: 行情 + 账户事件 + 目标 + 建议 + 运行记录 (M6 面板数据)."""
    from data_pipeline.accounting import AccountEvent, AccountService

    path = str(tmp_path / "account_dash.duckdb")
    db = DuckDBManager(path)
    db.init_database()
    df = make_ohlcv(SYMBOLS)
    db.upsert_ohlcv(df)
    days = sorted(df["date"].unique().to_list())
    first, last = days[0], days[-1]

    svc = AccountService(db)
    svc.ensure_account("BASE-B0", "B0 基准 (研究示例)", mode="backtest")
    svc.import_events([
        AccountEvent(event_id="b0-init", account_id="BASE-B0",
                     event_type="initial",
                     event_time=datetime(first.year, first.month, first.day),
                     amount=100000.0),
        AccountEvent(event_id="b0-buy", account_id="BASE-B0", event_type="trade",
                     event_time=datetime(first.year, first.month, first.day, 15),
                     symbol="510300", side="BUY", shares=10000.0, price=1.0,
                     fee=7.5),
    ])
    # 目标与建议 (关联账户)
    db.upsert_target(pl.DataFrame([{
        "target_id": "target-test", "strategy_id": "five-funnel",
        "strategy_version": "v0.2", "mode": "research",
        "as_of": last, "decision_time": datetime(last.year, last.month, last.day),
        "data_version": str(last), "pool_version": "uv-test",
        "config_version": "v0.2", "reason": "面板测试",
    }]))
    db.replace_target_weights("target-test", pl.DataFrame([
        {"target_id": "target-test", "symbol": "510300", "target_weight": 0.30},
        {"target_id": "target-test", "symbol": "510050", "target_weight": 0.10},
        {"target_id": "target-test", "symbol": "__CASH__", "target_weight": 0.60},
    ]))
    db.insert_proposals(pl.DataFrame([{
        "proposal_id": "P-dash-1", "target_id": "target-test",
        "account_id": "BASE-B0", "account_as_of": None,
        "symbol": "510300", "side": "SELL", "shares": 5000.0,
        "ref_price": 1.0, "ref_time": None, "est_amount": 5000.0,
        "est_fee": 3.75, "target_weight": 0.30, "actual_weight": 0.50,
        "valid_until": None, "priority": 50, "depends_on": None,
        "status": "proposed", "reason": "面板测试建议",
    }]))
    db.insert_run(pl.DataFrame([{
        "run_id": "daily-test", "run_type": "daily", "run_date": last,
        "mode": "research", "strategy_version": "v0.2",
        "data_version": str(last), "status": "ok",
        "detail_json": '{"signals": 3}', "started_at": datetime.now(),
        "finished_at": datetime.now(),
    }]))
    db.close()
    return path


class TestAccountViews:
    def test_account_summaries_include_backtest_mode(self, seeded_account_db):
        from dashboard.data_loader import load_account_summaries

        summaries = load_account_summaries(seeded_account_db)
        assert summaries.height == 1
        row = summaries.row(0, named=True)
        assert row["账户"] == "BASE-B0"
        assert row["模式"] == "模拟(回测)"
        # 期初 = 初始资本 − 费用 7.5 (起点日费用还原口径: nav_start=100000)
        assert row["期初"] == pytest.approx(100000.0)

    def test_nav_comparison_and_actual_vs_target(self, seeded_account_db):
        from dashboard.data_loader import (
            build_actual_vs_target,
            build_nav_comparison,
        )

        wide = build_nav_comparison(seeded_account_db)
        assert "BASE-B0" in wide.columns
        assert wide["BASE-B0"][0] == pytest.approx(100.0)  # 首日归一

        table = build_actual_vs_target(seeded_account_db, "BASE-B0")
        rows = {row["代码"]: row for row in table.iter_rows(named=True)}
        assert rows["510300"]["目标权重"] == "30.0%"
        assert rows["510300"]["实际权重"] != "-"
        # 偏离列存在; 现金行展示目标
        assert rows["__CASH__"]["目标权重"] == "60.0%"

    def test_pending_proposals_and_feedback_column(self, seeded_account_db):
        from dashboard.data_loader import load_pending_proposals

        pending = load_pending_proposals(seeded_account_db, "BASE-B0")
        assert pending.height == 1
        row = pending.row(0, named=True)
        assert row["方向"] == "SELL"
        assert row["状态"] == "proposed"
        assert row["反馈"] == "—"

    def test_latest_run_status(self, seeded_account_db):
        from dashboard.data_loader import load_latest_run

        run = load_latest_run(seeded_account_db)
        assert run is not None
        assert run["status"] == "ok"
        assert run["detail"]["signals"] == 3

    def test_read_only_connection_does_not_write(self, seeded_account_db):
        """只读连接: 面板读取不改变库文件内容 (C21)."""
        import os

        from dashboard.data_loader import (
            build_actual_vs_target,
            load_account_summaries,
        )

        before = os.path.getmtime(seeded_account_db)
        load_account_summaries(seeded_account_db)
        build_actual_vs_target(seeded_account_db, "BASE-B0")
        assert os.path.getmtime(seeded_account_db) == before

    def test_empty_accounts_return_empty_frames(self, seeded_db):
        from dashboard.data_loader import (
            build_actual_vs_target,
            build_nav_comparison,
            load_account_summaries,
            load_pending_proposals,
        )

        assert load_account_summaries(seeded_db).is_empty()
        assert build_nav_comparison(seeded_db).is_empty()
        assert build_actual_vs_target(seeded_db, None).is_empty()
        assert load_pending_proposals(seeded_db).is_empty()
