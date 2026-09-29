"""证券身份解析服务 (Phase 1.1 诊断, P11-M2-01).

职责:
  - 六位代码 → 交易所/类型/名称 的解析与缓存 (`securities` 表);
  - 名称模糊搜索 (探索入口候选);
  - 前缀推断仅作为"候选身份", 权威身份以名单接口 (股票全表/ETF 列表) 为准。

边界 (spec §8.4.4 / P11-M2):
  - 本模块只读写诊断身份缓存, 不写入 instruments / universe_members (策略池成员);
  - 名单接口失败时明确报错, 不猜测身份、不静默降级成"可用";
  - 名录缓存按 TTL 使用, 不随每次查询重复联网 (refresh=True 才强制重拉)。

数据来源 (P11-M1 实测, 2026-09-28):
  - 股票名录: akshare `stock_info_a_code_name` (5569 行, 含北交所 920xxx);
  - ETF 名录: akshare `fund_etf_category_sina(symbol="ETF基金")` (1687 行, sh/sz 前缀)。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

import polars as pl

if TYPE_CHECKING:
    from data_pipeline.duckdb_manager import DuckDBManager

logger = logging.getLogger(__name__)

# 前缀候选 (6 位代码; 权威身份仍以名单为准)
_SH_ETF_PREFIXES = ("51", "56", "58")
_SZ_ETF_PREFIXES = ("15", "16")
_SH_STOCK_PREFIXES = ("60", "68")
_SZ_STOCK_PREFIXES = ("00", "30")
_BJ_STOCK_PREFIXES = ("43", "83", "87", "88", "92")

_SOURCE_STOCK_LIST = "sina_stock_list"
_SOURCE_ETF_LIST = "sina_etf_list"

# 名录缓存有效期: 过期不主动重拉 (身份变动低频), 显式 refresh 才重拉
DEFAULT_TTL_DAYS = 30


class UnknownSymbolError(ValueError):
    """无法确认证券身份 (代码不存在 / 交易所无法确认)."""


class SecurityFetchError(RuntimeError):
    """名录接口失败 (网络/接口异常)."""


@dataclass(frozen=True)
class SecurityIdentity:
    """已确认的证券身份 (缓存记录)."""

    symbol: str
    name: str | None
    exchange: str          # SH / SZ / BJ
    sec_type: str          # stock / etf
    market_type: str | None  # A股 / 跨境 / 商品 (ETF; 池内以 instruments 为准)
    adjustment: str        # 研究价格口径: none(不复权) / hfq(后复权)
    source: str


# ------------------------------------------------------------------
# 纯函数: 代码规范化与候选推断
# ------------------------------------------------------------------

def normalize_symbol(raw: str) -> str:
    """规范化代码输入: 接受 '600519' / 'sh600519' / '600519.SH' → '600519'."""
    match = re.search(r"(\d{6})", str(raw).strip())
    if match is None:
        raise ValueError(f"无法解析证券代码: {raw!r} (需要 6 位数字)")
    return match.group(1)


def infer_exchange(symbol: str) -> str | None:
    """按前缀推断交易所候选 (无匹配 → None, 不猜测)."""
    code = normalize_symbol(symbol)
    if code.startswith(_SH_ETF_PREFIXES) or code.startswith(_SH_STOCK_PREFIXES):
        return "SH"
    if code.startswith(_SZ_ETF_PREFIXES) or code.startswith(_SZ_STOCK_PREFIXES):
        return "SZ"
    if code.startswith(_BJ_STOCK_PREFIXES):
        return "BJ"
    return None


def infer_sec_type(symbol: str) -> str:
    """按前缀推断证券类型候选 (stock / etf / unknown)."""
    code = normalize_symbol(symbol)
    if code.startswith(_SH_ETF_PREFIXES) or code.startswith(_SZ_ETF_PREFIXES):
        return "etf"
    if (
        code.startswith(_SH_STOCK_PREFIXES)
        or code.startswith(_SZ_STOCK_PREFIXES)
        or code.startswith(_BJ_STOCK_PREFIXES)
    ):
        return "stock"
    return "unknown"


def to_sina_symbol(symbol: str) -> str:
    """6 位代码 → 新浪前缀格式 (sh600519 / sz000001 / bj920992)."""
    code = normalize_symbol(symbol)
    exchange = infer_exchange(code)
    if exchange is None:
        raise UnknownSymbolError(f"无法推断交易所: {symbol}")
    return f"{exchange.lower()}{code}"


def default_adjustment(sec_type: str) -> str:
    """研究价格口径默认值: 股票=后复权 (EV-008 标准, 历史稳定可增量); ETF=不复权."""
    return "hfq" if sec_type == "stock" else "none"


# ------------------------------------------------------------------
# 名单获取 (懒加载 akshare; 可注入替身供测试)
# ------------------------------------------------------------------

def fetch_stock_list() -> pl.DataFrame:
    """股票名录: 交易所名称表优先, 失败回退新浪全量行情快照 (P11-M4 实测深交所站点可能超时).

    Returns: symbol/name/exchange/sec_type/adjustment/source (source 统一标 sina_stock_list,
    两种来源都只作"名称/代码"名录用途)。
    """
    import akshare as ak

    import data_pipeline.fetchers.akshare_fetcher  # noqa: F401  (代理绕过补丁, 与生产一致)

    rows: list[dict] = []
    try:
        pdf = ak.stock_info_a_code_name()
        if pdf is not None and not pdf.empty:
            for code_raw, name in zip(
                pdf["code"].tolist(), pdf["name"].tolist(), strict=False,
            ):
                if (row := _stock_row(str(code_raw), str(name))):
                    rows.append(row)
    except Exception as exc:
        logger.warning("交易所名称表获取失败 (%s: %s), 回退新浪全量快照",
                       type(exc).__name__, exc)

    if not rows:
        try:
            spot = ak.stock_zh_a_spot()
        except Exception as exc:
            raise SecurityFetchError(
                f"股票名录不可用: 名称表与新浪快照均失败 ({type(exc).__name__}: {exc})"
            ) from exc
        if spot is not None and not spot.empty:
            for code_raw, name in zip(
                spot["代码"].tolist(), spot["名称"].tolist(), strict=False,
            ):
                if (row := _stock_row(str(code_raw), str(name))):
                    rows.append(row)
    if not rows:
        raise SecurityFetchError("股票名录无有效 A 股记录")
    return pl.DataFrame(rows)


def _stock_row(code_raw: str, name: str) -> dict | None:
    """代码+名称 → 标准身份行 (前缀推断候选; B 股等非 A 股代码不纳入)."""
    try:
        code = normalize_symbol(code_raw)
    except ValueError:
        return None
    exchange = infer_exchange(code)
    if exchange is None:
        return None
    return {
        "symbol": code, "name": name, "exchange": exchange,
        "sec_type": "stock", "market_type": "A股",
        "adjustment": "hfq", "source": _SOURCE_STOCK_LIST,
    }


def fetch_etf_list() -> pl.DataFrame:
    """ETF 名录 (Sina ETF 分类) → 标准身份字段."""
    import akshare as ak

    import data_pipeline.fetchers.akshare_fetcher  # noqa: F401

    pdf = ak.fund_etf_category_sina(symbol="ETF基金")
    if pdf is None or pdf.empty:
        raise SecurityFetchError("fund_etf_category_sina 返回空名录")

    rows: list[dict] = []
    for code_raw, name in zip(
        pdf["代码"].tolist(), pdf["名称"].tolist(), strict=False,
    ):
        try:
            code = normalize_symbol(str(code_raw))
        except ValueError:
            continue
        exchange = infer_exchange(code)
        if exchange is None or infer_sec_type(code) != "etf":
            continue
        rows.append({
            "symbol": code, "name": str(name), "exchange": exchange,
            "sec_type": "etf", "market_type": None,
            "adjustment": "none", "source": _SOURCE_ETF_LIST,
        })
    if not rows:
        raise SecurityFetchError("ETF 名录无有效记录")
    return pl.DataFrame(rows)


# 名录列 schema (写入 securities 表)
_LIST_SCHEMA = {
    "symbol": pl.Utf8, "name": pl.Utf8, "exchange": pl.Utf8, "sec_type": pl.Utf8,
    "market_type": pl.Utf8, "adjustment": pl.Utf8, "source": pl.Utf8,
}


def _coerce_list_df(df: pl.DataFrame) -> pl.DataFrame:
    cols = []
    for col, dtype in _LIST_SCHEMA.items():
        cols.append(
            pl.col(col).cast(dtype) if col in df.columns
            else pl.lit(None, dtype=dtype).alias(col)
        )
    return df.select(cols)


# ------------------------------------------------------------------
# 解析器 (缓存 + 名录加载)
# ------------------------------------------------------------------

class SecurityResolver:
    """证券身份解析器 (securities 表缓存 + 名录按需加载).

    Usage:
        resolver = SecurityResolver(db)
        identity = resolver.resolve("600519")
        candidates = resolver.search("300")
    """

    def __init__(
        self,
        db: DuckDBManager,
        *,
        stock_list_fn: Callable[[], pl.DataFrame] | None = None,
        etf_list_fn: Callable[[], pl.DataFrame] | None = None,
        ttl_days: int = DEFAULT_TTL_DAYS,
    ) -> None:
        self.db = db
        self._stock_list_fn = stock_list_fn or fetch_stock_list
        self._etf_list_fn = etf_list_fn or fetch_etf_list
        self.ttl_days = ttl_days

    # ---- 公开接口 ----

    def resolve(self, raw_symbol: str, *, refresh: bool = False) -> SecurityIdentity:
        """解析证券身份; 缓存未命中时按需加载名录; 无法确认 → UnknownSymbolError."""
        code = normalize_symbol(raw_symbol)

        hit = self._from_cache(code)
        if hit is not None and not refresh:
            return self._enrich(hit)

        # 命中但显式 refresh: 重载对应名录后仍以缓存记录为准 (身份低频变化)
        if hit is not None and refresh:
            self._ensure_lists(refresh=True, want=infer_sec_type(code))
            return self._enrich(self._from_cache(code) or hit)

        # 未命中: 先按前缀加载对应名录 (避免不必要的全量拉取)
        want = infer_sec_type(code)
        self._ensure_lists(refresh=False, want=want)
        hit = self._from_cache(code)
        if hit is not None:
            return self._enrich(hit)

        # 仍未命中且名录已过期 (可能标的新上市): 尝试一次强制重载
        source = _SOURCE_ETF_LIST if want == "etf" else _SOURCE_STOCK_LIST
        age = self._list_age_days(source)
        if age is not None and age > self.ttl_days:
            logger.info("名录 %s 已过期 (%d 天), 未命中代码 %s → 强制重载", source, age, code)
            self._ensure_lists(refresh=True, want=want)
            hit = self._from_cache(code)
            if hit is not None:
                return self._enrich(hit)

        # 名录中无此代码 → 明确报错, 不猜测
        raise UnknownSymbolError(
            f"代码 {code} 未在股票/ETF 名录中找到 (可能不存在或非境内上市)"
        )

    def search(
        self, keyword: str, *, limit: int = 20, load_if_empty: bool = True,
    ) -> list[SecurityIdentity]:
        """名称模糊搜索 (缓存名录); 无缓存且允许时先加载两个名录."""
        text = str(keyword).strip()
        if not text:
            return []
        if load_if_empty and self._cache_is_empty():
            self._ensure_lists(refresh=False, want=None)
        df = self.db.find_securities_by_name(text, limit=limit)
        if df.is_empty():
            return []
        return [
            SecurityIdentity(
                symbol=row["symbol"], name=row.get("name"),
                exchange=row.get("exchange") or "SH",
                sec_type=row.get("sec_type") or "stock",
                market_type=row.get("market_type"),
                adjustment=row.get("adjustment") or "none",
                source=str(row.get("source") or "cache"),
            )
            for row in df.iter_rows(named=True)
        ]

    # ---- 内部 ----

    def _from_cache(self, code: str) -> SecurityIdentity | None:
        row = self.db.get_security(code)
        if row is None:
            return None
        sec_type = row.get("sec_type") or infer_sec_type(code)
        adjustment = row.get("adjustment")
        if not adjustment:
            adjustment = default_adjustment(sec_type) if sec_type != "unknown" else "none"
        return SecurityIdentity(
            symbol=row["symbol"],
            name=row.get("name"),
            exchange=row.get("exchange") or (infer_exchange(code) or "SH"),
            sec_type=sec_type,
            market_type=row.get("market_type"),
            adjustment=adjustment,
            source=str(row.get("source") or "cache"),
        )

    def _enrich(self, identity: SecurityIdentity) -> SecurityIdentity:
        """ETF 身份补充 market_type (池内以 instruments 为准; 不改写策略池)."""
        if identity.sec_type != "etf" or identity.market_type:
            return identity
        instruments = self.db.get_instruments([identity.symbol])
        if instruments.is_empty():
            return identity
        market_type = instruments["market_type"][0]
        if market_type:
            enriched = SecurityIdentity(
                symbol=identity.symbol, name=identity.name,
                exchange=identity.exchange, sec_type=identity.sec_type,
                market_type=market_type, adjustment=identity.adjustment,
                source=identity.source,
            )
            # 回填缓存 (便于后续直接读取)
            self.db.upsert_securities(pl.DataFrame([{
                "symbol": enriched.symbol, "market_type": market_type,
            }]))
            return enriched
        return identity

    def _cache_is_empty(self) -> bool:
        return self.db.get_table_count("securities") == 0

    def _list_age_days(self, source: str) -> int | None:
        row = self.db.conn.execute(
            "SELECT MAX(fetched_at) FROM securities WHERE source = ?", [source]
        ).fetchone()
        if row is None or row[0] is None:
            return None
        fetched: date = row[0].date() if hasattr(row[0], "date") else row[0]
        return (date.today() - fetched).days

    def _needs_load(self, source: str, *, refresh: bool) -> bool:
        """是否需要加载名录: 未加载过, 或显式刷新且非当天 (当天已加载不重复联网)."""
        age = self._list_age_days(source)
        return age is None or (refresh and age > 0)

    def _ensure_lists(self, *, refresh: bool, want: str | None = None) -> None:
        """按需加载名录 (want=None → 股票+ETF 都加载)."""
        if want in (None, "stock") and self._needs_load(_SOURCE_STOCK_LIST, refresh=refresh):
            self._load_one(_SOURCE_STOCK_LIST)
        if want in (None, "etf") and self._needs_load(_SOURCE_ETF_LIST, refresh=refresh):
            self._load_one(_SOURCE_ETF_LIST)
        if want == "unknown":
            # 前缀无法推断: 两个名录都加载后再判定
            self._ensure_lists(refresh=refresh, want=None)

    def _load_one(self, source: str) -> None:
        fn = self._stock_list_fn if source == _SOURCE_STOCK_LIST else self._etf_list_fn
        try:
            df = fn()
        except SecurityFetchError:
            raise
        except Exception as exc:
            raise SecurityFetchError(
                f"名录加载失败 ({source}): {type(exc).__name__}: {exc}"
            ) from exc
        if df is None or df.is_empty():
            raise SecurityFetchError(f"名录加载为空: {source}")
        self.db.upsert_securities(_coerce_list_df(df))
        logger.info("名录已缓存: %s (%d 行)", source, len(df))


def resolve_security(
    db: DuckDBManager, raw_symbol: str, *, refresh: bool = False,
    resolver: SecurityResolver | None = None,
) -> SecurityIdentity:
    """便捷函数: 解析单个证券身份."""
    resolver = resolver or SecurityResolver(db)
    return resolver.resolve(raw_symbol, refresh=refresh)


def resolver_from_settings(
    db: DuckDBManager, settings: dict | None = None, **kwargs,
) -> SecurityResolver:
    """从 settings.diagnostics 构造解析器 (TTL 等参数不硬编码)."""
    ttl = DEFAULT_TTL_DAYS
    if settings:
        ttl = int(
            settings.get("diagnostics", {}).get("securities_cache_ttl_days", ttl)
        )
    return SecurityResolver(db, ttl_days=ttl, **kwargs)
