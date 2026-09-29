"""统一诊断服务与快照状态机测试 (P11-M3-05/06; 注入替身, 不依赖网络)."""

import json
from datetime import date, datetime, timedelta
from datetime import time as dt_time

import polars as pl
import pytest

from data_pipeline.diag_data import FetchError, TradingCalendar, reset_calendar_cache
from data_pipeline.duckdb_manager import DuckDBManager
from data_pipeline.securities import SecurityIdentity
from orchestration.diagnosis_service import (
    TRIGGER_DATA_INVALID,
    TRIGGER_INITIAL,
    TRIGGER_MAINTAINED,
    TRIGGER_NEW_DEFENSE,
    TRIGGER_NEW_WATCH,
    TRIGGER_NOT_EVALUABLE,
    TRIGGER_PENDING_UPGRADE,
    TRIGGER_RECOVERED,
    TRIGGER_VERSION_CHANGE,
    get_snapshot_history,
    get_snapshot_payload,
    resolve_status_transition,
    run_diagnosis,
    snapshot_display_frame,
    watchlist_add,
    watchlist_remove,
    watchlist_update_note,
)
from strategies.diagnosis import DiagnosisAssessment, load_diagnostics_rules


@pytest.fixture(autouse=True)
def _clear_calendar_cache():
    reset_calendar_cache()
    yield
    reset_calendar_cache()


@pytest.fixture
def db(tmp_path):
    db = DuckDBManager(str(tmp_path / "svc.duckdb"))
    db.init_database()
    yield db
    db.close()


@pytest.fixture(scope="module")
def rules():
    return load_diagnostics_rules()


def _weekdays(start: date, n: int) -> list[date]:
    days, cur = [], start
    while len(days) < n:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def _prices(symbol: str, days: list[date], closes: list[float]) -> pl.DataFrame:
    return pl.DataFrame([
        {
            "symbol": symbol, "date": d, "open": c, "high": c * 1.001,
            "low": c * 0.999, "close": c, "volume": 1e6, "amount": 1e7,
        }
        for d, c in zip(days, closes, strict=True)
    ])


STOCK = SecurityIdentity(
    symbol="600519", name="贵州茅台", exchange="SH", sec_type="stock",
    market_type="A股", adjustment="hfq", source="test",
)
ETF = SecurityIdentity(
    symbol="512000", name="券商ETF", exchange="SH", sec_type="etf",
    market_type="A股", adjustment="none", source="test",
)


def _seed_identity(db: DuckDBManager, identity: SecurityIdentity) -> None:
    db.upsert_securities(pl.DataFrame([{
        "symbol": identity.symbol, "name": identity.name,
        "exchange": identity.exchange, "sec_type": identity.sec_type,
        "market_type": identity.market_type, "adjustment": identity.adjustment,
        "source": "test",
    }]))


class _Env:
    """可变的行情环境: 测试中随时改 closes / idx, fetcher 调用时读取最新值."""

    def __init__(self, n_days: int = 400, base: float = 100.0):
        self.days = _weekdays(date(2024, 1, 1), n_days)
        self.closes = [base] * n_days
        self.idx = n_days - 100  # 默认在倒数第 100 天

    @property
    def calendar(self) -> TradingCalendar:
        return TradingCalendar(tuple(self.days), "sina")

    def fetcher(self):
        def fetch(symbol: str) -> pl.DataFrame:
            i = self.idx
            return _prices(symbol, self.days[: i + 1], self.closes[: i + 1])
        return fetch

    def now(self) -> datetime:
        return datetime.combine(self.days[self.idx], dt_time(20, 0))


# ------------------------------------------------------------------
# 状态机单测 (纯函数; 合成快照/评估)
# ------------------------------------------------------------------

def _assessment(score: float | None, raw_status: str | None) -> DiagnosisAssessment:
    return DiagnosisAssessment(
        rules_version="r", as_of=None, raw_score=score, score=score,
        raw_status=raw_status, status=raw_status, completeness="5/5",
    )


def _prev(confirmed: str | None, raw: str | None, *,
          rules_version: str = "r", price_version: str = "p") -> dict:
    return {
        "status": confirmed, "rules_version": rules_version,
        "price_version": price_version,
        "metrics_json": json.dumps({"raw_status": raw}),
    }


class TestStatusTransition:
    def _run(self, prevs, score, raw, **kwargs):
        return resolve_status_transition(
            prevs, _assessment(score, raw),
            rules_version=kwargs.pop("rules_version", "r"),
            price_version=kwargs.pop("price_version", "p"),
            **kwargs,
        )

    def test_first_run_is_initial(self):
        assert self._run([], 0.0, "正常") == ("正常", TRIGGER_INITIAL)

    def test_not_evaluable_when_score_none(self):
        assert self._run([], None, None) == (None, TRIGGER_NOT_EVALUABLE)

    def test_version_change_does_not_fake_market_move(self):
        prevs = [_prev("正常", "正常", rules_version="old")]
        assert self._run(prevs, 60.0, "防御") == ("防御", TRIGGER_VERSION_CHANGE)
        prevs2 = [_prev("正常", "正常", price_version="other")]
        assert self._run(prevs2, 60.0, "防御") == ("防御", TRIGGER_VERSION_CHANGE)

    def test_prev_status_none_is_initial(self):
        prevs = [_prev(None, None)]
        assert self._run(prevs, 0.0, "正常") == ("正常", TRIGGER_INITIAL)

    def test_upgrade_needs_confirmation(self):
        prevs = [_prev("正常", "正常")]
        assert self._run(prevs, 70.0, "防御") == ("正常", TRIGGER_PENDING_UPGRADE)

    def test_upgrade_confirmed_on_second_day(self):
        prevs = [_prev("正常", "防御")]
        assert self._run(prevs, 70.0, "防御") == ("防御", TRIGGER_NEW_DEFENSE)
        prevs_watch = [_prev("正常", "警戒")]
        assert self._run(prevs_watch, 45.0, "警戒") == ("警戒", TRIGGER_NEW_WATCH)

    def test_recovery_confirms_immediately(self):
        prevs = [_prev("防御", "防御")]
        assert self._run(prevs, 0.0, "正常") == ("正常", TRIGGER_RECOVERED)

    def test_maintain(self):
        prevs = [_prev("警戒", "警戒")]
        assert self._run(prevs, 45.0, "警戒") == ("警戒", TRIGGER_MAINTAINED)

    def test_upgrade_confirmations_configurable(self):
        prevs = [_prev("正常", "正常")]
        assert self._run(prevs, 70.0, "防御", upgrade_confirmations=1) == (
            "防御", TRIGGER_NEW_DEFENSE,
        )


# ------------------------------------------------------------------
# 服务端到端 (股票 crash 场景: 正常 → 待确认 → 新增防御)
# ------------------------------------------------------------------

class TestServiceFullFlow:
    def test_transitions_and_idempotency(self, db, rules):
        env = _Env()
        fetch = env.fetcher()
        _seed_identity(db, STOCK)

        # day A: 平稳 100 → 正常 / 初始
        out1 = run_diagnosis(
            db, "600519", settings={}, now=env.now(), calendar=env.calendar,
            fetchers={"stock": fetch}, rules=rules,
        )
        assert out1.status == "正常"
        assert out1.trigger == TRIGGER_INITIAL
        assert out1.snapshot is not None
        assert db.get_table_count("diagnostic_snapshots") == 1

        # 同日重跑: 幂等 (不新增行), trigger 不与自己比较
        out1b = run_diagnosis(
            db, "600519", refresh="force", settings={}, now=env.now(),
            calendar=env.calendar, fetchers={"stock": fetch}, rules=rules,
        )
        assert out1b.trigger == TRIGGER_INITIAL
        assert db.get_table_count("diagnostic_snapshots") == 1

        # 制造暴跌: 270 日起 100 → 70 (跨过 skip 窗口起点)
        for i in range(270, len(env.closes)):
            env.closes[i] = 70.0

        # day B: 原始档位升到防御, 但确认档位保持正常 (待确认)
        env.idx += 1
        out2 = run_diagnosis(
            db, "600519", settings={}, now=env.now(), calendar=env.calendar,
            fetchers={"stock": fetch}, rules=rules,
        )
        assert out2.assessment.raw_status == "防御"
        assert out2.status == "正常"
        assert out2.trigger == TRIGGER_PENDING_UPGRADE

        # day C: 连续第 2 日防御 → 确认升级
        env.idx += 1
        out3 = run_diagnosis(
            db, "600519", settings={}, now=env.now(), calendar=env.calendar,
            fetchers={"stock": fetch}, rules=rules,
        )
        assert out3.status == "防御"
        assert out3.trigger == TRIGGER_NEW_DEFENSE
        assert db.get_table_count("diagnostic_snapshots") == 3

        # 读取接口: payload/history/列表
        payload = get_snapshot_payload(db, "600519")
        assert payload["status"] == "防御"
        assert payload["trigger"] == TRIGGER_NEW_DEFENSE
        assert payload["metrics"]["raw_status"] == "防御"
        assert payload["contributions"]["ma200_dev"]["points"] is not None
        history = get_snapshot_history(db, "600519")
        assert len(history) == 3
        assert history[0]["trigger"] == TRIGGER_NEW_DEFENSE
        frame = snapshot_display_frame(db)
        assert frame["symbol"].to_list() == ["600519"]

    def test_equity_event_pause_yields_no_score(self, db, rules):
        env = _Env(n_days=320, base=2.0)
        _seed_identity(db, ETF)
        db.upsert_equity_events(pl.DataFrame([{
            "symbol": "512000", "event_date": env.days[200],
            "event_type": "split_implied", "ratio": 2.0,
            "ratio_semantics": "price_factor", "source": "inferred_from_price",
            "inferred": True, "verified": False, "method": "test", "note": "test",
        }]))

        out = run_diagnosis(
            db, "512000", settings={}, now=env.now(), calendar=env.calendar,
            fetchers={"etf": env.fetcher()}, rules=rules,
        )
        assert out.equity.status == "paused"
        assert out.assessment.score is None
        assert out.status is None
        assert out.trigger == TRIGGER_NOT_EVALUABLE
        assert all("未核验" in m for m in out.assessment.missing_required)
        payload = get_snapshot_payload(db, "512000")
        assert payload["status"] is None
        assert payload["checks"]["unavailable"]

    def test_fetch_failure_records_data_invalid(self, db, rules):
        env = _Env(n_days=120)
        _seed_identity(db, STOCK)

        def _boom(symbol):
            raise FetchError("接口超时")

        out = run_diagnosis(
            db, "600519", settings={}, now=env.now(), calendar=env.calendar,
            fetchers={"stock": _boom}, rules=rules,
        )
        assert out.prices.status == "failed"
        assert out.status is None
        assert out.trigger == TRIGGER_DATA_INVALID
        payload = get_snapshot_payload(db, "600519")
        assert payload["trigger"] == TRIGGER_DATA_INVALID
        assert payload["completeness"] == "0/5"

    def test_short_history_not_evaluable(self, db, rules):
        env = _Env(n_days=120)
        env.idx = 99  # 仅 100 根 K 线 → MA200/动量缺失
        _seed_identity(db, STOCK)
        out = run_diagnosis(
            db, "600519", settings={}, now=env.now(), calendar=env.calendar,
            fetchers={"stock": env.fetcher()}, rules=rules,
        )
        assert out.assessment.score is None
        assert out.trigger == TRIGGER_NOT_EVALUABLE
        assert any("ma200" in m for m in out.assessment.missing_required)

    def test_no_trading_side_effects(self, db, rules):
        env = _Env()
        _seed_identity(db, STOCK)
        trading = (
            "accounts", "account_events", "position_lots", "targets",
            "trade_proposals", "execution_feedback", "trade_signals",
            "universe_members", "instruments", "strategy_states", "equity_events",
        )
        before = {t: db.get_table_count(t) for t in trading}
        run_diagnosis(
            db, "600519", settings={}, now=env.now(), calendar=env.calendar,
            fetchers={"stock": env.fetcher()}, rules=rules,
        )
        after = {t: db.get_table_count(t) for t in trading}
        assert before == after == {t: 0 for t in trading}


# ------------------------------------------------------------------
# 观察名单服务 (P11-M5-01; 受控写入口, 幂等, 不写策略池)
# ------------------------------------------------------------------

class TestWatchlistService:
    def test_add_captures_identity_and_is_idempotent(self, db):
        _seed_identity(db, STOCK)
        identity = watchlist_add(db, "600519", note="关注")
        assert identity.name == "贵州茅台"
        row = db.get_watchlist("active").filter(
            pl.col("symbol") == "600519"
        ).to_dicts()[0]
        assert row["name"] == "贵州茅台"
        assert row["exchange"] == "SH"
        assert row["note"] == "关注"
        watchlist_add(db, "sh600519")  # 重复添加 (带前缀) 幂等
        assert db.get_table_count("watchlist") == 1
        # 名单不写策略池
        assert db.get_table_count("instruments") == 0
        assert db.get_table_count("universe_members") == 0

    def test_remove_keeps_history(self, db):
        _seed_identity(db, STOCK)
        watchlist_add(db, "600519")
        watchlist_remove(db, "sh600519", reason="不再关注")
        assert db.get_watchlist("active").is_empty()
        assert db.get_watchlist("removed")["symbol"].to_list() == ["600519"]
        changes = db.get_watchlist_changes("600519")["action"].to_list()
        assert changes == ["remove", "add"]

    def test_note_update(self, db):
        _seed_identity(db, STOCK)
        watchlist_add(db, "600519")
        watchlist_update_note(db, "600519", "新备注")
        assert db.get_watchlist("active")["note"].to_list() == ["新备注"]
        with pytest.raises(ValueError):
            watchlist_update_note(db, "999999", "x")  # 名单外标的不存在

    def test_unknown_symbol_rejected(self, db):
        from data_pipeline.securities import SecurityResolver, UnknownSymbolError

        resolver = SecurityResolver(
            db,
            stock_list_fn=lambda: pl.DataFrame([
                {"symbol": "000001", "name": "平安银行", "exchange": "SZ",
                 "sec_type": "stock", "adjustment": "hfq", "source": "test"},
            ]),
            etf_list_fn=lambda: pl.DataFrame([
                {"symbol": "512000", "name": "券商ETF", "exchange": "SH",
                 "sec_type": "etf", "adjustment": "none", "source": "test"},
            ]),
        )
        with pytest.raises(UnknownSymbolError):
            watchlist_add(db, "600519", resolver=resolver)  # 名录中不存在 → 拒绝


# ------------------------------------------------------------------
# 条件检查接入 (P11-M4; 注入替身, 不打网络)
# ------------------------------------------------------------------

def _condition_period(year: int, month: int, **values) -> dict:
    day = {3: 31, 6: 30, 9: 30, 12: 31}[month]
    base = {
        "period": date(year, month, day), "announced": None, "revenue": None,
        "net_profit": None, "deducted_net_profit": None, "ocf": None,
        "total_assets": None, "total_liabilities": None, "equity_parent": None,
        "cash": None, "short_debt": None, "receivables": None,
    }
    base.update(values)
    return base


class TestConditionsInService:
    def _stock_extract(self) -> dict:
        return {
            "symbol": "600519", "is_financial": False, "errors": [],
            "periods": [
                _condition_period(2025, 6, revenue=1e10, net_profit=5e9, ocf=4.5e9,
                                  total_assets=1e11, total_liabilities=3e10,
                                  equity_parent=7e10, cash=2e10, short_debt=1e10),
                _condition_period(2026, 6, revenue=1.1e10, net_profit=5.5e9, ocf=5e9,
                                  total_assets=1.1e11, total_liabilities=3.3e10,
                                  equity_parent=7.7e10, cash=2.2e10, short_debt=1e10),
            ],
            "valuation": {
                "date": date(2026, 9, 29), "pe_ttm": 30.0, "pb": 6.0,
                "pe_percentile": 0.95, "pb_percentile": 0.5,
            },
        }

    def test_stock_conditions_persisted_in_snapshot(self, db, rules):
        env = _Env()
        _seed_identity(db, STOCK)
        run_diagnosis(
            db, "600519", settings={}, now=env.now(), calendar=env.calendar,
            fetchers={"stock": env.fetcher()}, rules=rules,
            condition_fetchers={"stock": lambda symbol: self._stock_extract()},
        )
        payload = get_snapshot_payload(db, "600519")
        conditions = payload["checks"]["conditions"]
        assert conditions["version"] == "cond-v0.1-research"
        assert "subject" in conditions
        by_id = {item["item_id"]: item for item in conditions["items"]}
        assert by_id["VAL-01"]["status"] == "关注"      # PE 分位 95% ≥ 90%
        assert by_id["SV-01"]["status"] == "未触发"      # 负债率 30%
        assert conditions["summary"]

    def test_condition_failure_does_not_block_technical(self, db, rules):
        env = _Env()
        _seed_identity(db, STOCK)

        def _boom(symbol):
            raise RuntimeError("接口超时")

        out = run_diagnosis(
            db, "600519", settings={}, now=env.now(), calendar=env.calendar,
            fetchers={"stock": env.fetcher()}, rules=rules,
            condition_fetchers={"stock": _boom},
        )
        assert out.assessment.score is not None          # 技术诊断不受影响
        payload = get_snapshot_payload(db, "600519")
        conditions = payload["checks"]["conditions"]
        assert conditions["summary"] == "无法评估"
        assert any("失败" in note for note in conditions["notes"])

    def test_etf_premium_unknown_in_snapshot(self, db, rules):
        env = _Env(n_days=320, base=2.0)
        _seed_identity(db, ETF)
        extract = {
            "symbol": "512000",
            "profile": {
                "full_name": "券商ETF", "fund_type": "指数型-股票",
                "inception": date(2016, 8, 1), "track_index": "中证券商指数",
                "benchmark": "", "manager": "管理人", "custodian": "托管人",
                "scale": {"value": 50.0, "unit": "亿元", "as_of": date(2026, 6, 30)},
                "management_fee": 0.008, "custody_fee": 0.001,  # 合计 0.90%/年 > 阈值
            },
            "premium": {
                "status": "unknown",
                "note": "当日净值缺失/未更新 或 市价缺失 → 折溢价无法评估 (不解除既有禁买)",
            },
            "spread": {"bid": 0.99, "ask": 1.0, "spread_pct": 0.01, "note": ""},
            "errors": [],
        }
        run_diagnosis(
            db, "512000", settings={}, now=env.now(), calendar=env.calendar,
            fetchers={"etf": env.fetcher()}, rules=rules,
            condition_fetchers={"etf": lambda symbol: extract},
        )
        payload = get_snapshot_payload(db, "512000")
        by_id = {item["item_id"]: item for item in payload["checks"]["conditions"]["items"]}
        assert by_id["PR-01"]["status"] == "无法评估"
        assert "不解除" in by_id["PR-01"]["detail"]
        assert by_id["EP-03"]["status"] == "关注"        # 合计费率 0.90%/年 > 0.60% 阈值
        assert by_id["TQ-01"]["status"] == "不适用"
