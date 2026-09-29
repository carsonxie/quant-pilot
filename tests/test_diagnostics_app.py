"""诊断模块 UI 测试 (P11-M5; AppTest 无头渲染 + 图表构造, 不触发网络)."""

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import polars as pl
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "quant_pilot.duckdb"
requires_db = pytest.mark.skipif(
    not DB_PATH.exists(), reason="真实数据库不存在",
)


def _radio_by_key(at, key: str):
    for widget in at.radio:
        if getattr(widget, "key", None) == key:
            return widget
    raise AssertionError(f"未找到 radio key={key}")


def _selectbox_by_key(at, key: str):
    for widget in at.selectbox:
        if getattr(widget, "key", None) == key:
            return widget
    raise AssertionError(f"未找到 selectbox key={key}")


class TestKlineFigure:
    def _frame(self, n: int = 40) -> pd.DataFrame:
        dates = pd.date_range("2026-05-01", periods=n, freq="B")
        closes = [10.0 + i * 0.05 for i in range(n)]
        return pd.DataFrame({
            "date": dates, "open": closes, "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes], "close": closes,
            "volume": [1e6] * n, "amount": [1e7] * n,
            "ma20": [10.5] * n, "ma60": [None] * n, "ma200": [None] * n,
            "boll_mid": [10.5] * n, "boll_upper": [11.0] * n, "boll_lower": [10.0] * n,
        })

    def test_traces_follow_toggles(self):
        from dashboard.diagnostics_app import _make_kline_figure

        fig = _make_kline_figure(
            self._frame(),
            show={"ma20": True, "ma60": False, "ma200": False, "boll": True},
            vol_mode="成交量",
        )
        names = [trace.name for trace in fig.data]
        assert "日K" in names
        assert "MA20" in names
        assert "MA60" not in names          # 开关关闭不渲染
        assert "成交量" in names
        assert sum(1 for name in names if name.startswith("BOLL")) == 3

    def test_amount_mode(self):
        from dashboard.diagnostics_app import _make_kline_figure

        fig = _make_kline_figure(
            self._frame(), show={}, vol_mode="成交额",
        )
        names = [trace.name for trace in fig.data]
        assert "成交额" in names
        assert "成交量" not in names


@requires_db
class TestDiagnosticsViewAppTest:
    def _app(self):
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(
            str(PROJECT_ROOT / "dashboard" / "app.py"), default_timeout=120,
        )
        at.run()
        assert not at.exception
        _radio_by_key(at, "app_module").set_value("单标的诊断")
        at.run()
        return at

    def test_module_switch_and_holdings_empty_state(self):
        at = self._app()
        assert not at.exception
        subheaders = " | ".join(s.value for s in at.subheader)
        assert "单标的诊断" in subheaders
        # 真实库 rebalancing.account=null → 未配置账户提示 (不生成伪持仓)
        assert any("未配置账户" in info.value for info in at.info)

    def test_watchlist_empty_state(self, tmp_path, monkeypatch):
        """空名单状态在隔离临时库验证 (真实库名单可随演示变化, 测试不依赖) ."""
        from streamlit.testing.v1 import AppTest

        from data_pipeline.duckdb_manager import DuckDBManager

        seed = DuckDBManager(str(tmp_path / "empty_wl.duckdb"))
        seed.init_database()
        seed.close()
        monkeypatch.setenv("QUANT_PILOT_DB", str(tmp_path / "empty_wl.duckdb"))

        at = AppTest.from_file(
            str(PROJECT_ROOT / "dashboard" / "app.py"), default_timeout=120,
        )
        at.run()
        _radio_by_key(at, "app_module").set_value("单标的诊断")
        at.run()
        _radio_by_key(at, "diag_view").set_value("观察名单")
        at.run()
        assert not at.exception
        assert any("观察名单为空" in info.value for info in at.info)

    def test_explore_does_not_fetch_without_submit(self):
        at = self._app()
        _radio_by_key(at, "diag_view").set_value("探索")
        at.run()
        assert not at.exception
        # 未提交表单 → 不触发身份解析 (无网络), 仅提示说明
        captions = " | ".join(c.value for c in at.caption)
        assert "不自动加入观察名单" in captions

    def test_account_panel_still_default_and_intact(self):
        at = self._app()  # 切回缺省模块验证既有账户视图保持
        _radio_by_key(at, "app_module").set_value("账户与信号")
        at.run()
        assert not at.exception
        subheaders = " | ".join(s.value for s in at.subheader)
        assert "实际 vs 目标" in subheaders
        assert "净值对照" in subheaders


class TestDetailRendersFromSnapshot:
    """详情页从既有快照渲染 (临时库 + 环境变量覆盖; 不打网络)."""

    def _seed(self, tmp_path: Path, as_of: date) -> str:
        from data_pipeline.duckdb_manager import DuckDBManager

        path = str(tmp_path / "detail.duckdb")
        db = DuckDBManager(path)
        db.init_database()
        db.add_watchlist("510300", name="沪深300ETF", exchange="SH", sec_type="etf")

        days: list[date] = []
        cursor = as_of - timedelta(days=380)
        while len(days) < 260:
            if cursor.weekday() < 5:
                days.append(cursor)
            cursor += timedelta(days=1)
        closes = [4.0 + 0.01 * i for i in range(len(days))]
        db.upsert_diag_prices(pl.DataFrame([
            {
                "symbol": "510300", "date": d, "open": c, "high": c * 1.01,
                "low": c * 0.99, "close": c, "volume": 1e6, "amount": 1e7,
                "source": "sina_etf", "adjustment": "none",
            }
            for d, c in zip(days, closes, strict=True)
        ]))
        db.upsert_diagnostic_snapshot({
            "snapshot_id": f"510300|{as_of}|diag-v0.1-research|diag_price_daily|none",
            "symbol": "510300", "as_of": as_of,
            "rules_version": "diag-v0.1-research", "price_version": "diag_price_daily|none",
            "run_kind": "live",
            "metrics_json": (
                '{"raw_status": "警戒", "raw_score": 30.0, '
                '"group_points": {"trend": 20.0}, '
                '"display": {"rsi14": 55.5, "boll_pos": 0.5}, '
                '"hints": [], "data_status": "fresh", '
                f'"data_last_date": "{as_of.isoformat()}", "history_days": 260}}'
            ),
            "contributions_json": (
                '{"ma200_dev": {"column": "ma200_dev", "value": -0.05, '
                '"points": 10.0, "max_points": 20, "triggered": true, "note": ""}}'
            ),
            "checks_json": (
                '{"missing_required": [], "unavailable": {}, "completeness": "5/5", '
                '"notes": [], "explanations_version": "tmpl-v0.1", '
                '"explanations": ["ma200_dev: 测试解释行"], '
                '"conditions": {"version": "cond-v0.1-research", '
                '"subject": "510300 沪深300ETF", "fetched_at": "2026-09-25T20:00:00", '
                '"summary": "关注 1 · 未触发 4", '
                '"items": [{"item_id": "EP-02", "label": "规模", "status": "关注", '
                '"detail": "净资产规模 4.00 亿元 (截止 2026-06-30; 季度口径)", '
                '"value": "4.00 亿元", "period": "2026-06-30", "published": null}], '
                '"notes": []}}'
            ),
            "score": 40.0, "status": "警戒", "completeness": "5/5", "trigger": "初始",
        })
        db.close()
        return path

    def test_watchlist_detail_renders_without_exception(self, tmp_path, monkeypatch):
        from streamlit.testing.v1 import AppTest

        as_of = date(2026, 9, 25)
        path = self._seed(tmp_path, as_of)

        import dashboard.diagnostics_app as diag_app

        monkeypatch.setenv("QUANT_PILOT_DB", path)
        # 快照视为最新 → 不发生自动刷新 (不打网络)
        monkeypatch.setattr(
            diag_app, "_expected_last_trading_day", lambda db_path: as_of,
        )

        at = AppTest.from_file(
            str(PROJECT_ROOT / "dashboard" / "app.py"), default_timeout=120,
        )
        at.run()
        assert not at.exception
        _radio_by_key(at, "app_module").set_value("单标的诊断")
        at.run()
        _radio_by_key(at, "diag_view").set_value("观察名单")
        at.run()
        _selectbox_by_key(at, "diag_watch_pick").set_value("510300")
        at.run()

        assert not at.exception
        labels = " | ".join(metric.label for metric in at.metric)
        assert "技术风险分 (0-100)" in labels
        assert "风险状态" in labels
        # 解释行来自快照 checks (无需重算)
        markdown_text = " | ".join(m.value for m in at.markdown)
        assert "测试解释行" in markdown_text
        # 条件检查区块 (P11-M4) 从快照渲染
        assert "条件检查" in markdown_text
        captions = " | ".join(c.value for c in at.caption)
        assert "cond-v0.1-research" in captions
        assert "关注 1" in captions
        # 列表与贡献表有内容
        assert len(at.dataframe) >= 1
