"""证券身份解析测试 (P11-M2-01; 不依赖网络: 名录函数注入替身)."""

import polars as pl
import pytest

from data_pipeline.duckdb_manager import DuckDBManager
from data_pipeline.securities import (
    SecurityFetchError,
    SecurityResolver,
    UnknownSymbolError,
    default_adjustment,
    infer_exchange,
    infer_sec_type,
    normalize_symbol,
    resolve_security,
    to_sina_symbol,
)


@pytest.fixture
def db(tmp_path):
    db = DuckDBManager(str(tmp_path / "sec.duckdb"))
    db.init_database()
    yield db
    db.close()


def _stock_list() -> pl.DataFrame:
    return pl.DataFrame([
        {"symbol": "600519", "name": "贵州茅台", "exchange": "SH", "sec_type": "stock",
         "market_type": "A股", "adjustment": "hfq", "source": "sina_stock_list"},
        {"symbol": "000001", "name": "平安银行", "exchange": "SZ", "sec_type": "stock",
         "market_type": "A股", "adjustment": "hfq", "source": "sina_stock_list"},
    ])


def _etf_list() -> pl.DataFrame:
    return pl.DataFrame([
        {"symbol": "510300", "name": "沪深300ETF", "exchange": "SH", "sec_type": "etf",
         "market_type": None, "adjustment": "none", "source": "sina_etf_list"},
        {"symbol": "159915", "name": "创业板ETF", "exchange": "SZ", "sec_type": "etf",
         "market_type": None, "adjustment": "none", "source": "sina_etf_list"},
    ])


class CountingFetcher:
    def __init__(self, factory):
        self.factory = factory
        self.calls = 0

    def __call__(self) -> pl.DataFrame:
        self.calls += 1
        return self.factory()


class TestPureFunctions:
    def test_normalize_accepts_common_forms(self):
        assert normalize_symbol("600519") == "600519"
        assert normalize_symbol("sh600519") == "600519"
        assert normalize_symbol("SH600519") == "600519"
        assert normalize_symbol(" 600519.SH ") == "600519"
        assert normalize_symbol("159915") == "159915"

    def test_normalize_rejects_invalid(self):
        with pytest.raises(ValueError):
            normalize_symbol("abc")
        with pytest.raises(ValueError):
            normalize_symbol("12345")

    def test_infer_exchange(self):
        assert infer_exchange("600519") == "SH"
        assert infer_exchange("688981") == "SH"
        assert infer_exchange("510300") == "SH"
        assert infer_exchange("000001") == "SZ"
        assert infer_exchange("300750") == "SZ"
        assert infer_exchange("159915") == "SZ"
        assert infer_exchange("920992") == "BJ"
        assert infer_exchange("900901") is None  # B 股不猜测

    def test_infer_sec_type(self):
        assert infer_sec_type("510300") == "etf"
        assert infer_sec_type("588000") == "etf"
        assert infer_sec_type("600519") == "stock"
        assert infer_sec_type("920992") == "stock"
        assert infer_sec_type("900901") == "unknown"

    def test_to_sina_symbol(self):
        assert to_sina_symbol("600519") == "sh600519"
        assert to_sina_symbol("000001") == "sz000001"
        assert to_sina_symbol("920992") == "bj920992"
        with pytest.raises(UnknownSymbolError):
            to_sina_symbol("900901")

    def test_default_adjustment(self):
        assert default_adjustment("stock") == "hfq"
        assert default_adjustment("etf") == "none"


class TestResolver:
    def _resolver(self, db):
        stock = CountingFetcher(_stock_list)
        etf = CountingFetcher(_etf_list)
        resolver = SecurityResolver(db, stock_list_fn=stock, etf_list_fn=etf)
        return resolver, stock, etf

    def test_resolve_stock_loads_list_once(self, db):
        resolver, stock, etf = self._resolver(db)
        identity = resolver.resolve("600519")
        assert identity.name == "贵州茅台"
        assert identity.exchange == "SH"
        assert identity.sec_type == "stock"
        assert identity.adjustment == "hfq"
        assert stock.calls == 1
        assert etf.calls == 0  # 股票前缀不拉 ETF 名录
        # 二次解析走缓存, 不再联网
        again = resolver.resolve("sh600519")
        assert again.symbol == "600519"
        assert stock.calls == 1

    def test_resolve_etf_and_enrich_market_type_from_instruments(self, db):
        db.upsert_instruments(pl.DataFrame([{
            "symbol": "513100", "name": "纳指ETF", "exchange": "SH",
            "market_type": "跨境", "asset_class": "权益",
        }]))
        etf_df = pl.DataFrame([
            {"symbol": "513100", "name": "纳指ETF", "exchange": "SH", "sec_type": "etf",
             "market_type": None, "adjustment": "none", "source": "sina_etf_list"},
        ])
        resolver = SecurityResolver(
            db, stock_list_fn=CountingFetcher(_stock_list),
            etf_list_fn=CountingFetcher(lambda: etf_df),
        )
        identity = resolver.resolve("513100")
        assert identity.sec_type == "etf"
        assert identity.market_type == "跨境"
        # 回填缓存: 再查直接可用
        assert db.get_security("513100")["market_type"] == "跨境"

    def test_resolve_unknown_raises_and_is_bounded(self, db):
        resolver, stock, etf = self._resolver(db)
        with pytest.raises(UnknownSymbolError):
            resolver.resolve("688888")  # 股票前缀, 名录中没有
        assert stock.calls == 1
        # 再次查询未知代码: 名录已缓存 (当天), 不再联网
        with pytest.raises(UnknownSymbolError):
            resolver.resolve("688888")
        assert stock.calls == 1

    def test_resolve_stale_list_forces_one_reload(self, db):
        resolver, stock, _ = self._resolver(db)
        resolver.resolve("600519")
        assert stock.calls == 1
        # 把名录时间改为 40 天前 → 未命中代码触发一次强制重载
        db.conn.execute(
            "UPDATE securities SET fetched_at = now() - INTERVAL 40 DAY "
            "WHERE source = 'sina_stock_list'"
        )
        with pytest.raises(UnknownSymbolError):
            resolver.resolve("688888")
        assert stock.calls == 2

    def test_refresh_same_day_does_not_hammer(self, db):
        resolver, stock, _ = self._resolver(db)
        resolver.resolve("600519")
        resolver.resolve("600519", refresh=True)
        assert stock.calls == 1  # 当天已加载, 显式刷新也不重复联网

    def test_search_candidates(self, db):
        resolver, stock, etf = self._resolver(db)
        candidates = resolver.search("银行")
        assert [c.symbol for c in candidates] == ["000001"]
        assert stock.calls == 1 and etf.calls == 1  # 缓存为空 → 两个名录都加载
        assert resolver.search("") == []

    def test_search_by_etf_name(self, db):
        resolver, _, _ = self._resolver(db)
        candidates = resolver.search("300", limit=10)
        assert {c.symbol for c in candidates} == {"510300"}

    def test_resolve_via_module_function(self, db):
        identity = resolve_security(db, "510300", resolver=SecurityResolver(
            db, stock_list_fn=_stock_list, etf_list_fn=_etf_list,
        ))
        assert identity.name == "沪深300ETF"


class TestListSchema:
    def test_coerce_fills_missing_columns(self, db):
        resolver = SecurityResolver(
            db, stock_list_fn=lambda: pl.DataFrame([
                {"symbol": "600519", "name": "贵州茅台"},  # 缺 exchange/sec_type 等
            ]),
            etf_list_fn=_etf_list,
        )
        identity = resolver.resolve("600519")
        assert identity.name == "贵州茅台"
        assert identity.exchange == "SH"       # 回退前缀推断
        assert identity.sec_type == "stock"
        assert identity.adjustment == "hfq"    # 名录缺口径 → 按类型默认 (股票=后复权)

    def test_empty_list_raises(self, db):
        resolver = SecurityResolver(
            db, stock_list_fn=lambda: pl.DataFrame(), etf_list_fn=_etf_list,
        )
        with pytest.raises(SecurityFetchError):
            resolver.resolve("600519")

    def test_resolver_from_settings_uses_ttl(self, db):
        from data_pipeline.securities import resolver_from_settings

        resolver = resolver_from_settings(
            db,
            {"diagnostics": {"securities_cache_ttl_days": 7}},
            stock_list_fn=_stock_list, etf_list_fn=_etf_list,
        )
        assert resolver.ttl_days == 7
        assert resolver.resolve("600519").name == "贵州茅台"

    def test_list_loader_exception_wrapped_as_fetch_error(self, db):
        def _boom():
            raise TimeoutError("www.szse.cn connect timeout")

        resolver = SecurityResolver(db, stock_list_fn=_boom, etf_list_fn=_etf_list)
        with pytest.raises(SecurityFetchError, match="名录加载失败"):
            resolver.resolve("600519")


class TestStockListFallback:
    """名录兜底: 交易所名称表失败 → 新浪全量快照 (P11-M4 实测深交所站点可能超时)."""

    def _patch(self, monkeypatch, name_table, spot):
        import akshare

        monkeypatch.setattr(akshare, "stock_info_a_code_name", name_table)
        monkeypatch.setattr(akshare, "stock_zh_a_spot", spot)

    def test_primary_source(self, monkeypatch):
        import pandas as pd

        from data_pipeline.securities import fetch_stock_list

        self._patch(
            monkeypatch,
            lambda: pd.DataFrame([
                {"code": "600519", "name": "贵州茅台"},
                {"code": "900901", "name": "云赛B股"},  # B 股 → 不纳入
            ]),
            lambda: pytest.fail("不应回退到快照"),
        )
        df = fetch_stock_list()
        assert df["symbol"].to_list() == ["600519"]
        assert df["exchange"].to_list() == ["SH"]

    def test_fallback_to_sina_spot(self, monkeypatch):
        import pandas as pd

        from data_pipeline.securities import fetch_stock_list

        def _boom():
            raise TimeoutError("szse down")

        self._patch(
            monkeypatch, _boom,
            lambda: pd.DataFrame([
                {"代码": "sh600519", "名称": "贵州茅台"},
                {"代码": "bj920000", "名称": "安徽凤凰"},
                {"代码": "sz300750", "名称": "宁德时代"},
            ]),
        )
        df = fetch_stock_list()
        assert set(df["symbol"]) == {"600519", "920000", "300750"}
        assert set(df["exchange"]) == {"SH", "BJ", "SZ"}

    def test_both_sources_fail(self, monkeypatch):
        from data_pipeline.securities import fetch_stock_list

        def _boom():
            raise TimeoutError("down")

        self._patch(monkeypatch, _boom, _boom)
        with pytest.raises(SecurityFetchError, match="均失败"):
            fetch_stock_list()
