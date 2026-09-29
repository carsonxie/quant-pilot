"""Phase 1.1 诊断数据服务 (P11-M2-03/04/05).

职责:
  - 交易日历 (新浪; 失败回退池内日期并集) 与"最近已完成交易日"判定;
  - 统一取数: 池内 ETF 读 `ohlcv_daily`; 非池标的读写 `diag_price_daily`;
  - 首查补足最长计算窗口; 新鲜度优先 (fresh 不重取); 增量 / 全量替换语义区分;
  - 刷新质检复用 validator (权益事件感知), critical 阻断不写入 (不绕过);
  - 权益事件状态: 未核验事件 / 未登记跳变 → 标记, 供 M3 暂停跨事件指标 (M2-04)。

口径 (P11-M1 实测选型):
  - ETF 日线: 新浪不复权 (`fund_etf_hist_sina`), adjustment="none";
  - 股票日线: 新浪后复权 (`stock_zh_a_daily(adjust="hfq")`), 历史稳定可增量;
  - 失败保留旧数据并标注 (stale/failed), 不静默、不编造。

边界: 本模块只写 ohlcv_daily / diag_price_daily (行情缓存) 与只读其他表,
不产生账户事件/目标/建议/执行反馈 (P11-M2-06)。
"""

from __future__ import annotations

import concurrent.futures
import logging
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from typing import TYPE_CHECKING

import polars as pl

from data_pipeline.processors.validator import QualityIssue, validate_ohlcv
from data_pipeline.securities import SecurityIdentity, to_sina_symbol

if TYPE_CHECKING:
    from data_pipeline.duckdb_manager import DuckDBManager

logger = logging.getLogger(__name__)

# ---- 默认参数 (可被 settings.diagnostics 覆盖) ----
DEFAULT_MIN_HISTORY_DAYS = 320
DEFAULT_REFETCH_BUFFER_DAYS = 10
DEFAULT_FETCH_TIMEOUT = 30.0
DEFAULT_FETCH_RETRIES = 3
DEFAULT_FETCH_BACKOFF = 2.0
DEFAULT_CUTOFF = "18:30"
CALENDAR_CACHE_TTL_SECONDS = 3600.0

_STANDARD_COLUMNS = ("symbol", "date", "open", "high", "low", "close", "volume", "amount")

_CALENDAR_CACHE: dict = {"fetched_at": 0.0, "calendar": None}


class FetchError(RuntimeError):
    """行情/名录接口获取失败 (超时/空响应/接口异常)."""


# ------------------------------------------------------------------
# 交易日历 (P11-M2-05)
# ------------------------------------------------------------------

@dataclass(frozen=True)
class TradingCalendar:
    """交易日历 (升序日期元组)."""

    days: tuple[date, ...]
    source: str  # sina / pool_fallback

    def contains(self, day: date) -> bool:
        return day in frozenset(self.days)

    @property
    def last_day(self) -> date | None:
        return self.days[-1] if self.days else None


def reset_calendar_cache() -> None:
    """清空进程内日历缓存 (测试/强制刷新用)."""
    _CALENDAR_CACHE["fetched_at"] = 0.0
    _CALENDAR_CACHE["calendar"] = None


def _fetch_trade_calendar() -> pl.DataFrame:
    """新浪交易日历 (含未来交易日; P11-M1 实测 8797 行)."""
    import akshare as ak

    import data_pipeline.fetchers.akshare_fetcher  # noqa: F401  (代理绕过补丁)

    return pl.from_pandas(ak.tool_trade_date_hist_sina())


def _extract_calendar_days(df: pl.DataFrame | None) -> list[date]:
    if df is None or df.is_empty():
        return []
    col = "trade_date" if "trade_date" in df.columns else (
        "date" if "date" in df.columns else df.columns[0]
    )
    values = df[col].to_list()
    days: set[date] = set()
    for value in values:
        if isinstance(value, date):
            days.add(value)
        else:
            try:
                days.add(date.fromisoformat(str(value)[:10]))
            except ValueError:
                continue
    return sorted(days)


def _pool_calendar_days(db: DuckDBManager | None) -> list[date]:
    """回退日历: 库内行情日期并集 (池内 ETF 近似交易日历)."""
    if db is None or not db.table_exists("ohlcv_daily"):
        return []
    rows = db.conn.execute(
        "SELECT DISTINCT date FROM ohlcv_daily ORDER BY date"
    ).fetchall()
    return [r[0] for r in rows if r[0] is not None]


def load_trading_calendar(
    *,
    db: DuckDBManager | None = None,
    fetcher: Callable[[], pl.DataFrame] | None = None,
    refresh: bool = False,
) -> TradingCalendar:
    """加载交易日历 (进程内缓存; 失败回退池内日期并集)."""
    cached: TradingCalendar | None = _CALENDAR_CACHE["calendar"]
    fresh_enough = (
        cached is not None
        and _time.monotonic() - _CALENDAR_CACHE["fetched_at"] < CALENDAR_CACHE_TTL_SECONDS
    )
    if cached is not None and fresh_enough and not refresh:
        return cached

    fn = fetcher or _fetch_trade_calendar
    try:
        days = _extract_calendar_days(fn())
        if not days:
            raise FetchError("交易日历返回空")
        calendar = TradingCalendar(days=tuple(days), source="sina")
    except Exception as exc:
        fallback = _pool_calendar_days(db)
        if not fallback:
            raise FetchError(f"交易日历不可用且无池内日期回退: {exc}") from exc
        logger.warning("交易日历获取失败, 回退池内日期并集: %s", exc)
        calendar = TradingCalendar(days=tuple(fallback), source="pool_fallback")

    _CALENDAR_CACHE["fetched_at"] = _time.monotonic()
    _CALENDAR_CACHE["calendar"] = calendar
    return calendar


def last_completed_trading_day(
    calendar: TradingCalendar,
    *,
    now: datetime | None = None,
    cutoff: str = DEFAULT_CUTOFF,
) -> date | None:
    """最近已完成交易日 (Asia/Shanghai 本地时间假设).

    规则: 当天是交易日且本地时间 ≥ cutoff → 当天; 否则取日历中 < 当天的最后一天。
    周末/节假日不在日历中, 不会误判为"数据延迟"。
    """
    now = now or datetime.now()
    today = now.date()
    cutoff_time = _parse_cutoff(cutoff)
    if today in frozenset(calendar.days) and now.time() >= cutoff_time:
        return today
    candidates = [d for d in calendar.days if d < today]
    return max(candidates) if candidates else None


def _parse_cutoff(cutoff: str) -> dt_time:
    try:
        return dt_time.fromisoformat(str(cutoff))
    except ValueError:
        logger.warning("无法解析 cutoff=%r, 使用默认 %s", cutoff, DEFAULT_CUTOFF)
        return dt_time.fromisoformat(DEFAULT_CUTOFF)


# ------------------------------------------------------------------
# 接口调用 (超时/重试/失败归一)
# ------------------------------------------------------------------

def _call_with_timeout(fn: Callable[[], object], timeout: float, desc: str) -> object:
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(fn)
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        raise FetchError(f"{desc}: 超时 (>{timeout:.0f}s)") from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _retry_call(
    fn: Callable[[], object],
    *,
    desc: str,
    retries: int = DEFAULT_FETCH_RETRIES,
    backoff: float = DEFAULT_FETCH_BACKOFF,
    timeout: float = DEFAULT_FETCH_TIMEOUT,
) -> object:
    """指数退避重试; 已知"代码不可查询"错误不重试 (P11-M1 实测 KeyError)."""
    last: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            return _call_with_timeout(fn, timeout, desc)
        except FetchError as exc:
            last = exc
        except KeyError as exc:
            raise FetchError(f"{desc}: 代码不可查询或接口结构异常 ({exc})") from exc
        except Exception as exc:
            last = FetchError(f"{desc}: {type(exc).__name__}: {exc}")
        if attempt < max(1, retries):
            sleep_s = backoff * (2 ** (attempt - 1))
            logger.warning("%s 第 %d 次失败, %.1fs 后重试: %s", desc, attempt, sleep_s, last)
            _time.sleep(sleep_s)
    assert last is not None
    raise last


def _normalize_hist(
    df: pl.DataFrame,
    symbol: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> pl.DataFrame:
    """标准化为 [symbol, date, open, high, low, close, volume, amount] (日期升序)."""
    if df.is_empty():
        return pl.DataFrame()
    if "date" in df.columns:
        df = df.with_columns(
            pl.col("date").cast(pl.Utf8).str.replace_all("-", "")
            .str.strptime(pl.Date, "%Y%m%d", strict=False)
        )
    columns = []
    for col in _STANDARD_COLUMNS:
        if col == "symbol":
            columns.append(pl.lit(symbol).alias("symbol"))
        elif col == "date":
            columns.append(pl.col("date"))
        elif col in df.columns:
            columns.append(pl.col(col).cast(pl.Float64, strict=False).alias(col))
        else:
            columns.append(pl.lit(None, dtype=pl.Float64).alias(col))
    out = df.select(columns).drop_nulls(subset=["date"]).sort("date")
    if start is not None:
        out = out.filter(pl.col("date") >= start)
    if end is not None:
        out = out.filter(pl.col("date") <= end)
    return out


def fetch_etf_daily(
    symbol: str,
    *,
    start: date | None = None,
    end: date | None = None,
    retries: int = DEFAULT_FETCH_RETRIES,
    backoff: float = DEFAULT_FETCH_BACKOFF,
    timeout: float = DEFAULT_FETCH_TIMEOUT,
) -> pl.DataFrame:
    """ETF 日线 (新浪, 不复权; 全历史后按范围过滤)."""

    def _do() -> pl.DataFrame:
        import akshare as ak

        import data_pipeline.fetchers.akshare_fetcher  # noqa: F401

        pdf = ak.fund_etf_hist_sina(symbol=to_sina_symbol(symbol))
        if pdf is None or pdf.empty:
            raise FetchError(f"{symbol}: ETF 日线返回空 (代码可能不存在或已退市)")
        return _normalize_hist(pl.from_pandas(pdf), symbol, start=start, end=end)

    result = _retry_call(
        _do, desc=f"ETF 日线 {symbol}", retries=retries, backoff=backoff, timeout=timeout,
    )
    assert isinstance(result, pl.DataFrame)
    return result


def fetch_stock_daily(
    symbol: str,
    *,
    adjust: str = "hfq",
    retries: int = DEFAULT_FETCH_RETRIES,
    backoff: float = DEFAULT_FETCH_BACKOFF,
    timeout: float = DEFAULT_FETCH_TIMEOUT,
) -> pl.DataFrame:
    """股票日线 (新浪; adjust='hfq' 后复权, 历史稳定)."""

    def _do() -> pl.DataFrame:
        import akshare as ak

        import data_pipeline.fetchers.akshare_fetcher  # noqa: F401

        pdf = ak.stock_zh_a_daily(symbol=to_sina_symbol(symbol), adjust=adjust)
        if pdf is None or pdf.empty:
            raise FetchError(f"{symbol}: 股票日线返回空 (代码可能不存在或已退市)")
        return _normalize_hist(pl.from_pandas(pdf), symbol)

    result = _retry_call(
        _do, desc=f"股票日线 {symbol}", retries=retries, backoff=backoff, timeout=timeout,
    )
    assert isinstance(result, pl.DataFrame)
    return result


# ------------------------------------------------------------------
# 涨跌幅限制 (产品感知; 与 validator / settings 口径一致)
# ------------------------------------------------------------------

def price_limit_pct(symbol: str, sec_type: str) -> float:
    """基础涨跌幅限制 (比例): BJ 30%; 创业板/科创板 20%; 其余 10%; ETF 默认 10%."""
    code = str(symbol)
    if sec_type == "stock":
        if code.startswith(("43", "83", "87", "88", "92")):
            return 0.30
        if code.startswith(("300", "301", "688", "689")):
            return 0.20
    return 0.10


def _limit_and_buffer(symbol: str, sec_type: str, settings: dict | None) -> tuple[float, float]:
    overrides = {}
    buffer = 0.02
    if settings:
        dq = settings.get("data_quality", {}).get("daily_change", {})
        overrides = {
            str(k): float(v) / 100.0 for k, v in dq.get("limit_overrides_pct", {}).items()
        }
        buffer = float(dq.get("review_buffer_pct", 2.0)) / 100.0
    limit = overrides.get(str(symbol), price_limit_pct(symbol, sec_type))
    return limit, buffer


# ------------------------------------------------------------------
# 取数结果数据结构
# ------------------------------------------------------------------

@dataclass
class ResearchPrices:
    """统一取数结果 (M2-03)."""

    symbol: str
    prices: pl.DataFrame
    source: str                 # ohlcv_daily / diag_price_daily
    adjustment: str             # none / hfq
    last_date: date | None
    expected_last: date | None
    status: str                 # fresh/updated/stale/empty/failed/blocked
    notes: list[str] = field(default_factory=list)
    history_days: int = 0

    @property
    def is_fresh(self) -> bool:
        return self.status in ("fresh", "updated")

    @property
    def short_history(self) -> bool:
        return 0 < self.history_days < DEFAULT_MIN_HISTORY_DAYS

    @property
    def price_version(self) -> str:
        """价格口径版本 (源+复权口径).

        诊断日期由快照 `as_of` 承载; 常规数据更新 (新交易日) 不属于"版本变化",
        只有口径/来源改变 (如复权修订、数据源切换) 才算版本差异 (M3-05 修正)。
        """
        return f"{self.source}|{self.adjustment}"


@dataclass
class EquityEventCheck:
    """权益事件状态 (M2-04; 供 M3 决定跨事件指标暂停/调整)."""

    status: str                 # ok / paused / data_issue / not_applicable
    registered: list[dict]
    unverified: list[dict]
    unregistered_jumps: list[dict]
    adjusted_prices: pl.DataFrame | None
    note: str


# ------------------------------------------------------------------
# 统一取数 (M2-03)
# ------------------------------------------------------------------

def pool_symbols_from_settings(settings: dict | None) -> set[str]:
    """settings.universe 中的池内标的代码集合."""
    if not settings:
        return set()
    universe = settings.get("universe", {}) or {}
    symbols: set[str] = set()
    for category in universe.values():
        if isinstance(category, list):
            for item in category:
                if isinstance(item, dict) and item.get("symbol"):
                    symbols.add(str(item["symbol"]))
    return symbols


def _read_store(db: DuckDBManager, symbol: str, is_pool: bool) -> pl.DataFrame:
    df = db.get_ohlcv(symbols=[symbol]) if is_pool else db.get_diag_prices(symbol)
    if df.is_empty():
        return df
    columns = [c for c in _STANDARD_COLUMNS if c in df.columns]
    return df.select(columns).sort("date")


def _is_pool_symbol(
    db: DuckDBManager, identity: SecurityIdentity, pools: set[str],
) -> bool:
    """池内判定: 标的在 settings 池中, 或库内已有 ohlcv 数据 (含出池旧持仓).

    P11-D2/P11-A2: 出池/禁买持仓仍需可诊断 — 只要 ohlcv_daily 有历史即走池内路径。
    """
    if identity.sec_type != "etf":
        return False
    if identity.symbol in pools:
        return True
    row = db.conn.execute(
        "SELECT 1 FROM ohlcv_daily WHERE symbol = ? LIMIT 1", [identity.symbol]
    ).fetchone()
    return row is not None


def _fetch_for_identity(
    identity: SecurityIdentity,
    *,
    start: date | None,
    fetchers: dict[str, Callable] | None,
    settings: dict | None,
) -> pl.DataFrame:
    fn = (fetchers or {}).get(identity.sec_type)
    if fn is not None:
        return fn(identity.symbol)
    retries = int(_cfg(settings, "fetch_retries", DEFAULT_FETCH_RETRIES))
    backoff = float(_cfg(settings, "fetch_backoff_base", DEFAULT_FETCH_BACKOFF))
    timeout = float(_cfg(settings, "fetch_timeout_seconds", DEFAULT_FETCH_TIMEOUT))
    if identity.sec_type == "etf":
        return fetch_etf_daily(
            identity.symbol, start=start,
            retries=retries, backoff=backoff, timeout=timeout,
        )
    return fetch_stock_daily(
        identity.symbol, adjust=identity.adjustment or "hfq",
        retries=retries, backoff=backoff, timeout=timeout,
    )


def _gate_window(
    fetched: pl.DataFrame, last_cached: date | None, *, tail_n: int = 15,
) -> pl.DataFrame:
    """质检门窗口: 增量只查新增行; 首次取数查近端 tail_n 行.

    历史段的权益跳变由 `check_equity_events` 处理 (暂停/登记流程),
    不为历史异常阻断当前数据更新 (与 daily 增量质检哲学一致)。
    """
    if last_cached is not None:
        gate = fetched.filter(pl.col("date") > last_cached)
    else:
        gate = fetched.tail(tail_n)
    return gate


def _validate_refresh(
    gate: pl.DataFrame,
    identity: SecurityIdentity,
    db: DuckDBManager,
    settings: dict | None,
) -> list[QualityIssue]:
    if gate.is_empty():
        return []
    limit, buffer = _limit_and_buffer(identity.symbol, identity.sec_type, settings)
    registered = db.get_equity_events([identity.symbol])
    if registered is not None and registered.is_empty():
        registered = None
    return validate_ohlcv(
        gate,
        price_limits={identity.symbol: limit},
        default_price_limit=limit,
        price_limit_buffer=buffer,
        registered_equity_events=registered,
    )


def _write_fetch(
    db: DuckDBManager,
    identity: SecurityIdentity,
    fetched: pl.DataFrame,
    *,
    is_pool: bool,
) -> None:
    if is_pool:
        db.upsert_ohlcv(fetched.select(_STANDARD_COLUMNS))
        return
    enriched = fetched.with_columns([
        pl.lit("sina_etf" if identity.sec_type == "etf" else "sina_stock").alias("source"),
        pl.lit(identity.adjustment or "none").alias("adjustment"),
    ])
    if identity.sec_type == "stock":
        # 后复权序列修订时历史整体重算 → 全量替换, 不做新旧拼接 (spec §8.4.3-4)
        db.replace_diag_prices(identity.symbol, enriched)
    else:
        db.upsert_diag_prices(enriched)


def ensure_research_prices(
    db: DuckDBManager,
    identity: SecurityIdentity,
    *,
    refresh: str = "auto",
    min_history_days: int | None = None,
    settings: dict | None = None,
    now: datetime | None = None,
    calendar: TradingCalendar | None = None,
    fetchers: dict[str, Callable] | None = None,
    pool_symbols: set[str] | None = None,
) -> ResearchPrices:
    """统一取数入口 (新鲜度优先; 失败保留旧数据).

    Args:
        refresh: "none" 只读 | "auto" 过期才刷新 (默认) | "force" 强制刷新
        fetchers: 测试注入 {"etf": fn(symbol), "stock": fn(symbol), "calendar": fn()}
        pool_symbols: 池内标的集合 (None → 从 settings 取)
        now: 注入当前时间 (测试; 默认 datetime.now())
    """
    if refresh not in ("none", "auto", "force"):
        raise ValueError(f"refresh 必须为 none/auto/force, 得到 {refresh!r}")

    min_days = min_history_days or _cfg(settings, "min_history_days", DEFAULT_MIN_HISTORY_DAYS)
    buffer_days = int(_cfg(settings, "refetch_buffer_days", DEFAULT_REFETCH_BUFFER_DAYS))

    cal = calendar or load_trading_calendar(
        db=db, fetcher=(fetchers or {}).get("calendar"),
    )
    expected = last_completed_trading_day(
        cal, now=now, cutoff=str(_cfg(settings, "session_close_cutoff", DEFAULT_CUTOFF)),
    )

    pools = pool_symbols if pool_symbols is not None else pool_symbols_from_settings(settings)
    is_pool = _is_pool_symbol(db, identity, pools)

    prices = _read_store(db, identity.symbol, is_pool)
    last_cached: date | None = prices["date"].max() if not prices.is_empty() else None

    notes: list[str] = []
    if expected is None and not prices.is_empty():
        notes.append("交易日历无法给出最近已完成交易日, 新鲜度未评估")
    if prices.is_empty():
        status = "empty"
    elif expected is not None and last_cached is not None and last_cached < expected:
        status = "stale"
    else:
        status = "fresh"

    should_fetch = refresh == "force" or (
        refresh == "auto" and status in ("empty", "stale")
    )
    if should_fetch:
        start = None
        if last_cached is not None and identity.sec_type == "etf":
            start = last_cached - timedelta(days=buffer_days)
        try:
            fetched = _fetch_for_identity(
                identity, start=start, fetchers=fetchers, settings=settings,
            )
        except FetchError as exc:
            if prices.is_empty():
                notes.append(str(exc))
                status = "failed"
            else:
                notes.append(f"刷新失败, 沿用缓存数据: {exc}")
                status = "stale"
        except Exception as exc:  # 注入替身的意外异常也必须可见
            notes.append(f"取数异常: {type(exc).__name__}: {exc}")
            status = "failed" if prices.is_empty() else "stale"
        else:
            new_rows = (
                fetched
                if last_cached is None
                else fetched.filter(pl.col("date") > last_cached)
            )
            if fetched.is_empty() or new_rows.is_empty():
                notes.append("接口无新增数据 (可能非交易日)")
                if not prices.is_empty():
                    status = "stale"
            else:
                gate = _gate_window(fetched, last_cached)
                issues = _validate_refresh(gate, identity, db, settings)
                criticals = [i for i in issues if i.severity == "critical"]
                if criticals:
                    status = "blocked"
                    notes.append(
                        "质检阻断, 未写入: "
                        + "; ".join(i.detail for i in criticals[:3])
                    )
                else:
                    _write_fetch(db, identity, fetched, is_pool=is_pool)
                    prices = _read_store(db, identity.symbol, is_pool)
                    last_cached = prices["date"].max() if not prices.is_empty() else None
                    status = "updated"

    history_days = prices.height
    if 0 < history_days < min_days:
        notes.append(
            f"历史仅 {history_days} 个交易日 (< {min_days}); 长窗口指标不可用"
        )

    return ResearchPrices(
        symbol=identity.symbol,
        prices=prices,
        source="ohlcv_daily" if is_pool else "diag_price_daily",
        adjustment=identity.adjustment or "none",
        last_date=last_cached,
        expected_last=expected,
        status=status,
        notes=notes,
        history_days=history_days,
    )


def _cfg(settings: dict | None, key: str, default):
    if not settings:
        return default
    return settings.get("diagnostics", {}).get(key, default)


# ------------------------------------------------------------------
# 权益事件状态 (M2-04)
# ------------------------------------------------------------------

def detect_unregistered_jumps(
    prices: pl.DataFrame,
    identity: SecurityIdentity,
    *,
    settings: dict | None,
    registered: list[dict] | None = None,
) -> list[dict]:
    """检测窗口内未登记的疑似权益事件跳变 (超过产品涨跌幅限制+容差)."""
    if prices.height < 3:
        return []
    limit, buffer = _limit_and_buffer(identity.symbol, identity.sec_type, settings)
    registered_dates = [
        row["event_date"] for row in (registered or []) if row.get("event_date")
    ]
    df = prices.sort("date").with_columns(
        (pl.col("close") / pl.col("close").shift(1) - 1.0).alias("_ret")
    )
    jumps = df.filter(pl.col("_ret").abs() > (limit + buffer))
    out: list[dict] = []
    for row in jumps.iter_rows(named=True):
        day = row["date"]
        if any(abs((day - reg).days) <= 1 for reg in registered_dates):
            continue  # 已登记事件附近, 不重复计入
        out.append({
            "date": day.isoformat(),
            "ret": round(float(row["_ret"]), 4),
        })
    return out


def adjust_prices_for_events(
    prices: pl.DataFrame, events: list[dict],
) -> pl.DataFrame:
    """按已核验事件生成连续价格序列 (回看调整; 仅调 OHLC, 量额保持原始口径).

    事件语义 (equity_events.ratio_semantics): "后一收盘 ≈ 前一收盘 / ratio";
    事件日之前的价格乘以 1/ratio (多事件按日序累乘), 消除跳变。
    """
    adjusted = prices.sort("date")
    for event in sorted(events, key=lambda e: e["event_date"]):
        ratio = float(event.get("ratio") or 0.0)
        if ratio <= 0:
            continue
        event_day = event["event_date"]
        adjusted = adjusted.with_columns([
            pl.when(pl.col("date") < event_day)
            .then(pl.col(col) / ratio)
            .otherwise(pl.col(col))
            .alias(col)
            for col in ("open", "high", "low", "close")
        ])
    return adjusted


def check_equity_events(
    db: DuckDBManager,
    identity: SecurityIdentity,
    prices: pl.DataFrame,
    *,
    settings: dict | None = None,
) -> EquityEventCheck:
    """权益事件状态 (窗口内).

    - 后复权 (股票): not_applicable (序列已含调整);
    - 存在未核验事件 → paused (M3 暂停跨事件指标);
    - 存在未登记跳变 → data_issue (在登记流程处理前不可信);
    - 全部已核验 → ok 并给出调整序列 (adjusted_prices)。
    """
    if identity.adjustment == "hfq":
        return EquityEventCheck(
            status="not_applicable", registered=[], unverified=[],
            unregistered_jumps=[], adjusted_prices=None,
            note="后复权序列已含分红/拆分调整",
        )
    if prices.is_empty():
        return EquityEventCheck(
            status="ok", registered=[], unverified=[], unregistered_jumps=[],
            adjusted_prices=None, note="无行情数据",
        )

    raw_events = db.get_equity_events([identity.symbol])
    first, last = prices["date"].min(), prices["date"].max()
    registered: list[dict] = []
    if not raw_events.is_empty():
        registered = [
            row for row in raw_events.iter_rows(named=True)
            if first <= row["event_date"] <= last
        ]
    unverified = [e for e in registered if not e.get("verified")]
    jumps = detect_unregistered_jumps(
        prices, identity, settings=settings, registered=registered,
    )

    adjusted_prices = None
    if jumps:
        status = "data_issue"
        note = f"窗口内 {len(jumps)} 处未登记跳变 (疑似权益事件), 相关指标不可信"
    elif unverified:
        status = "paused"
        note = f"窗口内 {len(unverified)} 条未核验权益事件, 跨事件指标应暂停并说明"
    else:
        status = "ok"
        note = "窗口内无未核验事件" + (
            f"; {len(registered)} 条已核验事件已用于价格调整" if registered else ""
        )
        if registered:
            adjusted_prices = adjust_prices_for_events(prices, registered)

    return EquityEventCheck(
        status=status, registered=registered, unverified=unverified,
        unregistered_jumps=jumps, adjusted_prices=adjusted_prices, note=note,
    )
