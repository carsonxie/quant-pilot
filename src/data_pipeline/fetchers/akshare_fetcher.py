"""Akshare 数据获取适配器 — A 股 ETF 日线 OHLCV.

双数据源支持:
  - Sina (新浪财经): 主力源, 稳定无封禁, 不复权数据
  - Eastmoney (东方财富): 备用源, 后复权数据, 含反封锁措施

统一接口: BaseFetcher → fetch(symbols, start, end) → pl.DataFrame
"""

from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from typing import Optional

import akshare as ak
import polars as pl
import requests as _requests

logger = logging.getLogger(__name__)

# ---- 代理绕过: 使用 trust_env=False 的 Session ----
# 部分 Windows 环境存在不可达的系统代理配置,
# 导致 eastmoney API 请求失败. 使用独立 Session 绕过.
_proxy_free_session = _requests.Session()
_proxy_free_session.trust_env = False

_original_requests_get = _requests.get


def _patched_get(url: str, **kwargs) -> _requests.Response:
    """使用 trust_env=False 的 Session 发送 GET 请求."""
    return _proxy_free_session.get(url, **kwargs)


_requests.get = _patched_get

# ---- 反封锁: User-Agent 轮换池 (仅 eastmoney 使用) ----
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

# ---- 列名映射 ----
# eastmoney 中文列名 → 标准英文列名
_EM_COLUMN_MAP: dict[str, str] = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "涨跌幅": "pct_change",
    "涨跌额": "change_amount",
    "换手率": "turnover_rate",
}

_REQUIRED_COLS = ["date", "open", "high", "low", "close", "volume"]

# ---- Sina 额外列 (归一化时丢弃) ----
_SINA_EXTRA_COLS = {"prevclose", "postVol", "postAmt"}

# ---- 标的代码 → 交易所前缀 ----
# ETF 规则: 51/56/58 开头 → 上海 (sh), 15/16 开头 → 深圳 (sz)
_SH_PREFIXES = ("5", "6", "9")  # 上海交易所 ETF 代码首位
_SZ_PREFIXES = ("0", "1", "2", "3")  # 深圳交易所 ETF 代码首位


class BaseFetcher(ABC):
    """数据获取适配器抽象基类.

    所有数据源 (Sina/Eastmoney/JQData/Tushare) 需实现此接口,
    确保上层代码不依赖具体数据源.
    """

    @abstractmethod
    def fetch(
        self,
        symbols: list[str],
        start_date: str,
        end_date: str,
    ) -> pl.DataFrame:
        """拉取多只标的的日线数据.

        Args:
            symbols: 标的代码列表
            start_date: 起始日期 "YYYYMMDD"
            end_date: 截止日期 "YYYYMMDD"

        Returns:
            Polars DataFrame, 列: symbol, date, open, high, low, close, volume, amount
        """
        ...


class AkshareFetcher(BaseFetcher):
    """ETF 日线数据获取器, 支持双数据源.

    数据源:
      - sina (默认): 新浪财经, 快速稳定无封禁; 返回不复权数据,
        ETF 分红影响微小 (年化 1-2%), P0 可接受; P1 将实现后复权 DIY 计算
      - eastmoney: 东方财富, 后复权数据; 存在 IP 限流风险,
        含反封锁休眠 (模拟人类行为) + 指数退避重试

    Usage:
        fetcher = AkshareFetcher(source="sina")
        df = fetcher.fetch(["510300", "510050"], "20190101", "20241231")
    """

    def __init__(
        self,
        source: str = "sina",
        min_sleep: float = 0.1,
        max_sleep: float = 0.5,
        max_retries: int = 3,
        retry_backoff_base: float = 5.0,
    ) -> None:
        """初始化获取器.

        Args:
            source: 数据源 "sina" (新浪) | "eastmoney" (东方财富)
            min_sleep: API 调用间最小休眠秒数 (eastmoney 反封锁)
            max_sleep: API 调用间最大休眠秒数 (eastmoney 反封锁)
            max_retries: 单只标的失败最大重试次数
            retry_backoff_base: 重试指数退避基数 (秒)
        """
        self.source = source
        self.min_sleep = min_sleep
        self.max_sleep = max_sleep
        self.max_retries = max_retries
        self.retry_backoff_base = retry_backoff_base

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def fetch(
        self,
        symbols: list[str],
        start_date: str,
        end_date: str,
    ) -> pl.DataFrame:
        """拉取多只 ETF 的日线历史数据.

        对每只 ETF 独立调用, 单只失败不中断整体流程.

        Args:
            symbols: ETF 代码列表, 如 ["510300", "159915"]
            start_date: 起始日期 "YYYYMMDD"
            end_date: 截止日期 "YYYYMMDD"

        Returns:
            合并后的 Polars DataFrame

        Raises:
            RuntimeError: 所有标的均获取失败时抛出
        """
        all_frames: list[pl.DataFrame] = []
        failed: list[str] = []

        total = len(symbols)
        logger.info(
            "Starting batch fetch [%s]: %d symbols, %s → %s",
            self.source, total, start_date, end_date,
        )

        for i, symbol in enumerate(symbols, 1):
            logger.info("[%d/%d] Fetching %s ...", i, total, symbol)
            try:
                df = self._fetch_single(symbol, start_date, end_date)
                if not df.is_empty():
                    all_frames.append(df)
                    logger.info(
                        "[%d/%d] %s: %d rows fetched",
                        i, total, symbol, len(df),
                    )
                else:
                    logger.warning("[%d/%d] %s: got empty DataFrame", i, total, symbol)
                    failed.append(symbol)
            except Exception:
                logger.exception("[%d/%d] %s: fetch failed", i, total, symbol)
                failed.append(symbol)

        if failed:
            logger.warning(
                "Failed/empty symbols (%d/%d): %s",
                len(failed), total, failed,
            )

        if not all_frames:
            raise RuntimeError(
                f"All {total} symbols failed to fetch. "
                f"Check network or akshare API availability."
            )

        result = pl.concat(all_frames)
        logger.info(
            "Batch fetch complete: %d rows, %d/%d symbols OK",
            len(result), len(all_frames), total,
        )
        return result

    # ------------------------------------------------------------------
    # 单只 ETF 获取
    # ------------------------------------------------------------------

    def _fetch_single(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pl.DataFrame:
        """拉取单只 ETF 日线数据.

        根据 self.source 分发到对应 API.
        """
        if self.source == "sina":
            return self._fetch_via_sina(symbol, start_date, end_date)
        else:
            return self._fetch_via_eastmoney(symbol, start_date, end_date)

    # ------------------------------------------------------------------
    # Sina 路径 (主力)
    # ------------------------------------------------------------------

    def _fetch_via_sina(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pl.DataFrame:
        """通过新浪财经获取数据.

        Sina 不限流, 无需反封锁休眠; fund_etf_hist_sina 不支持日期过滤,
        返回全量历史数据, 需在内存中按日期截断.
        """
        sina_sym = self._to_sina_symbol(symbol)
        logger.debug("%s → sina symbol: %s", symbol, sina_sym)

        raw = self._call_sina(sina_sym)
        if raw.is_empty():
            return pl.DataFrame()

        # 按日期范围截断
        df = self._filter_by_date(raw, start_date, end_date)
        return self._normalize(df, symbol)

    @staticmethod
    def _to_sina_symbol(symbol: str) -> str:
        """将纯数字代码转换为 Sina 格式 (带交易所前缀).

        510300 → sh510300, 159915 → sz159915

        ETF 代码规则:
          - 51xxxx / 56xxxx / 58xxxx → 上海交易所 → sh 前缀
          - 15xxxx / 16xxxx → 深圳交易所 → sz 前缀
        """
        code = symbol.zfill(6)
        first_char = code[0]
        if first_char in _SH_PREFIXES:
            return f"sh{code}"
        elif first_char in _SZ_PREFIXES:
            return f"sz{code}"
        else:
            # 兜底: 无法判断时假设上海 (更常见)
            logger.warning("Unknown exchange for %s, assuming Shanghai", symbol)
            return f"sh{code}"

    @staticmethod
    def _call_sina(sina_symbol: str) -> pl.DataFrame:
        """调用 fund_etf_hist_sina 获取全量历史数据.

        Sina 返回 pandas DataFrame, 列已是英文 (date/open/high/low/close/volume/amount),
        可能包含额外列: prevclose, postVol, postAmt.
        """
        pdf = ak.fund_etf_hist_sina(symbol=sina_symbol)

        if pdf is None or pdf.empty:
            logger.info("%s: sina returned empty DataFrame", sina_symbol)
            return pl.DataFrame()

        return pl.from_pandas(pdf)

    @staticmethod
    def _filter_by_date(
        df: pl.DataFrame,
        start_date: str,
        end_date: str,
    ) -> pl.DataFrame:
        """按日期范围过滤 DataFrame.

        Sina 不支持 API 级别日期过滤, 返回全量历史 → 在此截断.
        """
        if "date" not in df.columns:
            return df

        # date 列可能是 pandas datetime.date → Polars Date, 也可能是字符串
        # 统一转为字符串比较
        date_col = pl.col("date")
        date_str = date_col.cast(pl.Utf8).str.replace_all("-", "")

        return df.filter(
            (date_str >= start_date) & (date_str <= end_date)
        )

    # ------------------------------------------------------------------
    # Eastmoney 路径 (备用, 含反封锁)
    # ------------------------------------------------------------------

    def _fetch_via_eastmoney(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pl.DataFrame:
        """通过东方财富获取数据, 含重试与反封锁休眠.

        Raises:
            RuntimeError: 所有重试均失败
        """
        last_error: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                # 反封锁: 每次请求前随机休眠
                self._random_sleep()

                df = self._call_eastmoney(symbol, start_date, end_date)
                return self._normalize(df, symbol)

            except Exception as e:
                last_error = e
                if attempt < self.max_retries:
                    backoff = self.retry_backoff_base * (2 ** (attempt - 1))
                    logger.warning(
                        "%s attempt %d/%d failed: %s. Backoff %.0fs...",
                        symbol, attempt, self.max_retries, e, backoff,
                    )
                    time.sleep(backoff)
                else:
                    logger.error(
                        "%s: all %d attempts failed. Last error: %s",
                        symbol, self.max_retries, e,
                    )

        raise RuntimeError(
            f"Failed to fetch {symbol} after {self.max_retries} attempts"
        ) from last_error

    @staticmethod
    def _call_eastmoney(
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pl.DataFrame:
        """调用 fund_etf_hist_em (eastmoney 后端) 获取原始数据.

        返回后复权 (hfq) 数据 — 量化研究标准:
          后复权保留最早的绝对价格为真实交易价格,
          后续价格按分红/拆分因子等比放大,
          确保对数收益率计算正确且价格序列始终为正.
        """
        code = symbol.zfill(6)

        pdf = ak.fund_etf_hist_em(
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="hfq",  # 后复权 — 量化研究标准
        )

        if pdf is None or pdf.empty:
            logger.info("%s: eastmoney returned empty DataFrame", symbol)
            return pl.DataFrame()

        return pl.from_pandas(pdf)

    # ------------------------------------------------------------------
    # 反封锁工具 (仅 eastmoney)
    # ------------------------------------------------------------------

    def _random_sleep(self) -> None:
        """随机休眠, 模拟人类浏览行为, 防止 eastmoney 封 IP."""
        duration = random.uniform(self.min_sleep, self.max_sleep)
        logger.debug("Anti-blocking sleep: %.2fs", duration)
        time.sleep(duration)

    @staticmethod
    def _get_random_ua() -> str:
        """从 User-Agent 池中随机选取一个."""
        return random.choice(_USER_AGENTS)

    # ------------------------------------------------------------------
    # 归一化 (两个源共用)
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(raw: pl.DataFrame, symbol: str) -> pl.DataFrame:
        """将原始输出标准化为统一 schema.

        支持双源:
          - eastmoney: 中文列名 → 英文列名; 日期格式 "2024-01-15"
          - sina: 列名已是英文; 日期可能是 Date 或字符串
        """
        if raw.is_empty():
            return pl.DataFrame()

        logger.debug("%s raw columns: %s", symbol, raw.columns)

        # 中文列名 → 英文列名 (eastmoney); 对 sina 是 no-op
        rename_map = {
            cn: en
            for cn, en in _EM_COLUMN_MAP.items()
            if cn in raw.columns
        }
        df = raw.rename(rename_map)

        # 添加 symbol 列
        df = df.with_columns(pl.lit(symbol).alias("symbol"))

        # 日期解析: 统一转为 Date 类型
        if "date" in df.columns:
            df = df.with_columns(
                pl.col("date")
                .cast(pl.Utf8)
                .str.replace_all("-", "")
                .str.strptime(pl.Date, "%Y%m%d", strict=False)
            )

        # 确保数值列为 Float64
        numeric_cols = ["open", "high", "low", "close", "volume", "amount"]
        for col in numeric_cols:
            if col in df.columns:
                df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
            else:
                df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias(col))

        # 标准列序 (丢弃 prevclose, postVol, postAmt 等额外列)
        output_cols = ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
        return df.select(output_cols)


# ------------------------------------------------------------------
# CLI 入口 (uv run fetch-data)
# ------------------------------------------------------------------

def main() -> None:
    """CLI: 从 settings.yaml 读取标的池, 全量拉取历史数据并入库."""
    import sys
    from pathlib import Path

    import yaml

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # 定位项目根目录
    project_root = Path(__file__).resolve().parents[3]

    # 读取配置
    settings_path = project_root / "config" / "settings.yaml"
    if not settings_path.exists():
        logger.error("settings.yaml not found at %s", settings_path)
        sys.exit(1)

    with open(settings_path, encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    # 收集所有标的
    universe = settings.get("universe", {})
    all_etfs: list[dict] = []
    for category in universe.values():
        if isinstance(category, list):
            all_etfs.extend(category)

    symbols = [etf["symbol"] for etf in all_etfs]
    logger.info("Universe loaded: %d ETFs from settings.yaml", len(symbols))

    # 配置获取器
    fetch_cfg = settings.get("data_fetch", {})
    source = fetch_cfg.get("source", "sina")
    fetcher = AkshareFetcher(
        source=source,
        min_sleep=fetch_cfg.get("min_sleep", 0.1),
        max_sleep=fetch_cfg.get("max_sleep", 0.5),
        max_retries=fetch_cfg.get("max_retries", 3),
        retry_backoff_base=fetch_cfg.get("retry_backoff_base", 5.0),
    )

    start_date = fetch_cfg.get("default_start_date", "20190101")
    end_date = time.strftime("%Y%m%d")

    # 拉取数据
    try:
        df = fetcher.fetch(symbols, start_date, end_date)
        logger.info("Fetch complete: %d rows", len(df))

        # 全量导入不阻断, 但输出产品感知质检摘要 (历史含已知权益事件跳变;
        # 明细由 validator 日志给出 — C10)
        from data_pipeline.processors.validator import validate_ohlcv

        issues = validate_ohlcv(df)
        n_critical = sum(1 for i in issues if i.severity == "critical")
        if n_critical:
            logger.warning(
                "全量导入质检: %d 个阻断级问题 (历史权益事件/疑似脏数据, "
                "不阻断导入; 每日增量流程会按阻断处理, 明细见上方日志)",
                n_critical,
            )

        # 入库
        db_path = project_root / settings["storage"]["db_path"]
        from data_pipeline.duckdb_manager import DuckDBManager

        db = DuckDBManager(str(db_path))
        db.init_database()

        # 写入 ETF 元数据
        meta_rows = [
            {
                "symbol": etf["symbol"],
                "name": etf["name"],
                "market_type": etf["market_type"],
                "settlement_type": etf["settlement"],
                "list_date": None,
                "avg_daily_volume_20d": None,
            }
            for etf in all_etfs
        ]
        db.upsert_etf_metadata(pl.DataFrame(meta_rows))

        # 写入 OHLCV
        db.upsert_ohlcv(df)

        count = db.get_table_count("ohlcv_daily")
        logger.info("Database ready: %d rows in ohlcv_daily", count)
        db.close()

    except Exception:
        logger.exception("Fetch failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
