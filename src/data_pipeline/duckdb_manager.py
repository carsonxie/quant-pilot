"""DuckDB Manager — 单一真理之源 (Single Source of Truth).

负责 Quant-Pilot 所有持久化数据的读写操作:
  - 数据库初始化与 schema 版本管理
  - OHLCV 日线数据的幂等 UPSERT
  - ETF 元数据维护
  - 查询接口 (供策略/回测层调用)

规则:
  - 这是 DuckDB 的**唯一写入入口**, 禁止其他模块直连 .duckdb 文件
  - 所有写入操作必须幂等 — 重复执行不产生重复行/副作用
  - 连接生命周期由本模块管理, 调用方通过上下文管理器获取连接
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Generator, Optional

import duckdb
import polars as pl

logger = logging.getLogger(__name__)

# ---- Schema 版本 ----
# v1: P0 MVP 6 表; v2: trade_signals.strategy_version (旧记录标注 v0.1, A0-1);
# v3: 账户/事件/批次/快照 + 四类记录 + 运行/实验/迁移日志 (M2);
# v4: instruments/universe_versions/universe_members/nav_daily (M3 四层池);
# v5: strategy_states (持有期高水位/棘轮止损/冷却状态, M4-06)
SCHEMA_VERSION = 5

# ---- DDL (建表语句) ----
_DDL_OHLCV = """
CREATE TABLE IF NOT EXISTS ohlcv_daily (
    symbol    VARCHAR NOT NULL,
    date      DATE NOT NULL,
    open      DOUBLE,
    high      DOUBLE,
    low       DOUBLE,
    close     DOUBLE,
    volume    DOUBLE,
    amount    DOUBLE,
    adj_factor DOUBLE DEFAULT 1.0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, date)
)
"""

_DDL_ETF_META = """
CREATE TABLE IF NOT EXISTS etf_metadata (
    symbol               VARCHAR PRIMARY KEY,
    name                 VARCHAR NOT NULL,
    market_type          VARCHAR,
    settlement_type      VARCHAR,
    list_date            DATE,
    avg_daily_volume_20d DOUBLE,
    updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_DDL_MACRO = """
CREATE TABLE IF NOT EXISTS macro_indicators (
    indicator_name VARCHAR NOT NULL,
    stat_date      DATE NOT NULL,
    publish_date   DATE NOT NULL,
    value          DOUBLE,
    source         VARCHAR,
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (indicator_name, stat_date)
)
"""

_DDL_SIGNALS = """
CREATE TABLE IF NOT EXISTS trade_signals (
    signal_id      BIGINT PRIMARY KEY,
    signal_date    DATE NOT NULL,
    symbol         VARCHAR NOT NULL,
    direction      VARCHAR NOT NULL,
    target_weight  DOUBLE,
    trigger_reason VARCHAR,
    strategy_version VARCHAR,
    executed       BOOLEAN DEFAULT FALSE,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_DDL_ACCOUNT = """
CREATE TABLE IF NOT EXISTS account_state (
    as_of_date         DATE PRIMARY KEY,
    total_nav          DOUBLE,
    cash               DOUBLE,
    positions_json     VARCHAR,
    daily_return       DOUBLE,
    cumulative_return  DOUBLE,
    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_DDL_SCHEMA_VER = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    description VARCHAR
)
"""

# ---- v3: 账户、四类记录与运行记录 (M2) ----

_DDL_ACCOUNTS = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id    VARCHAR PRIMARY KEY,
    name          VARCHAR,
    mode          VARCHAR NOT NULL,          -- real / paper / backtest (spec §3.1)
    base_currency VARCHAR DEFAULT 'CNY',
    note          VARCHAR,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_DDL_ACCOUNT_EVENTS = """
CREATE TABLE IF NOT EXISTS account_events (
    event_id      VARCHAR PRIMARY KEY,       -- 稳定去重标识 (外部编号或确定性哈希)
    account_id    VARCHAR NOT NULL,
    event_type    VARCHAR NOT NULL,          -- initial/deposit/withdrawal/trade/fee/
                                             -- dividend_accrual/dividend_payment/
                                             -- share_change/correction
    event_time    TIMESTAMP NOT NULL,        -- 事件发生时间 (与 import_time 分开)
    import_time   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    symbol        VARCHAR,
    side          VARCHAR,                   -- BUY / SELL
    shares        DOUBLE,
    price         DOUBLE,
    amount        DOUBLE,
    fee           DOUBLE,
    source        VARCHAR,                   -- manual / csv / broker / simulated
    reference     VARCHAR,                   -- 成交编号等外部去重键
    correction_of VARCHAR,                   -- 更正事件指向原 event_id
    payload_json  VARCHAR,                   -- 结构化扩展 (initial 持仓列表 / share_change 等)
    note          VARCHAR
)
"""

_DDL_POSITION_LOTS = """
CREATE TABLE IF NOT EXISTS position_lots (
    lot_id        VARCHAR PRIMARY KEY,
    account_id    VARCHAR NOT NULL,
    symbol        VARCHAR NOT NULL,
    open_event_id VARCHAR,
    shares        DOUBLE NOT NULL,           -- 剩余份额 (派生缓存, 权威来源=事件)
    cost_basis    DOUBLE,                    -- 剩余成本 (含费)
    open_date     DATE,
    sellable_date DATE,                      -- 可卖起始日 (批次级)
    closed        BOOLEAN DEFAULT FALSE,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_DDL_ACCOUNT_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS account_snapshots (
    account_id        VARCHAR NOT NULL,
    as_of             TIMESTAMP NOT NULL,    -- 估值时点
    cash              DOUBLE,
    cash_withdrawable DOUBLE,
    cash_reserved     DOUBLE,
    receivable        DOUBLE,
    positions_json    VARCHAR,               -- 批次摘要
    event_cutoff      VARCHAR,               -- 回放所用最后 event_id
    reconciled        BOOLEAN DEFAULT FALSE,
    note              VARCHAR,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, as_of)
)
"""

_DDL_TARGETS = """
CREATE TABLE IF NOT EXISTS targets (
    target_id        VARCHAR PRIMARY KEY,
    strategy_id      VARCHAR,
    strategy_version VARCHAR,
    mode             VARCHAR,
    as_of            DATE,
    decision_time    TIMESTAMP,
    data_version     VARCHAR,
    pool_version     VARCHAR,
    config_version   VARCHAR,
    reason           VARCHAR,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_DDL_TARGET_WEIGHTS = """
CREATE TABLE IF NOT EXISTS target_weights (
    target_id     VARCHAR NOT NULL,
    symbol        VARCHAR NOT NULL,
    target_weight DOUBLE,
    PRIMARY KEY (target_id, symbol)
)
"""

_DDL_TRADE_PROPOSALS = """
CREATE TABLE IF NOT EXISTS trade_proposals (
    proposal_id    VARCHAR PRIMARY KEY,
    target_id      VARCHAR,
    account_id     VARCHAR,
    account_as_of  TIMESTAMP,                -- 建议引用账户状态时点
    symbol         VARCHAR,
    side           VARCHAR,
    shares         DOUBLE,
    ref_price      DOUBLE,
    ref_time       TIMESTAMP,
    est_amount     DOUBLE,
    est_fee        DOUBLE,
    target_weight  DOUBLE,
    actual_weight  DOUBLE,
    valid_until    TIMESTAMP,
    priority       INTEGER,
    depends_on     VARCHAR,                  -- 卖出依赖 (proposal_id)
    status         VARCHAR,                  -- proposed/partial/filled/expired/replaced/rejected
    reason         VARCHAR,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_DDL_EXECUTION_FEEDBACK = """
CREATE TABLE IF NOT EXISTS execution_feedback (
    feedback_id   VARCHAR PRIMARY KEY,
    proposal_id   VARCHAR,                   -- 允许 NULL (标记非系统交易)
    account_id    VARCHAR,
    trade_id      VARCHAR,                   -- 关联成交/事件编号
    feedback_time TIMESTAMP,
    status        VARCHAR,                   -- filled/partial/rejected/delayed/not_executed
    shares        DOUBLE,
    price         DOUBLE,
    fee           DOUBLE,
    source        VARCHAR,
    reason        VARCHAR,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_DDL_RUNS = """
CREATE TABLE IF NOT EXISTS runs (
    run_id           VARCHAR PRIMARY KEY,
    run_type         VARCHAR,                -- daily / backtest / migration / import
    run_date         DATE,
    mode             VARCHAR,
    strategy_version VARCHAR,
    data_version     VARCHAR,                -- 数据截止
    status           VARCHAR,                -- ok / blocked / failed
    push_ok          BOOLEAN,
    push_attempts    INTEGER,
    push_error       VARCHAR,
    detail_json      VARCHAR,
    started_at       TIMESTAMP,
    finished_at      TIMESTAMP
)
"""

_DDL_EXPERIMENTS = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id   VARCHAR PRIMARY KEY,
    kind            VARCHAR,
    code_version    VARCHAR,
    data_version    VARCHAR,
    pool_version    VARCHAR,
    config_json     VARCHAR,
    cost_scenario   VARCHAR,
    purpose         VARCHAR,
    result_location VARCHAR,
    status          VARCHAR,
    conclusion      VARCHAR,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_DDL_SCHEMA_MIGRATION_LOG = """
CREATE TABLE IF NOT EXISTS schema_migration_log (
    version      INTEGER NOT NULL,
    applied_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    description  VARCHAR,
    backup_ref   VARCHAR,
    checks_json  VARCHAR,
    status       VARCHAR DEFAULT 'applied'
)
"""

# ---- v4: 产品契约 / 池版本 / NAV (M3) ----

_DDL_INSTRUMENTS = """
CREATE TABLE IF NOT EXISTS instruments (
    symbol                 VARCHAR PRIMARY KEY,
    name                   VARCHAR,
    exchange               VARCHAR,
    market_type            VARCHAR,          -- A股 / 跨境 / 商品 / 债券
    asset_class            VARCHAR,          -- 权益 / 商品 / 固收
    index_name             VARCHAR,          -- 跟踪指数 (研究元数据)
    fund_type              VARCHAR DEFAULT 'ETF',
    currency               VARCHAR DEFAULT 'CNY',
    underlying_market      VARCHAR,          -- 底层市场 (A股/港股/美股/黄金现货)
    list_date              DATE,             -- 上市日 (缺失=按数据起点近似, 标 source)
    delist_date            DATE,
    settlement_type        VARCHAR,          -- T+0 / T+1
    price_limit_pct        DOUBLE,           -- 涨跌幅限制 (10/20)
    premium_check_required BOOLEAN DEFAULT FALSE,  -- 溢价检查为必需 (unknown → 禁新买)
    rule_note              VARCHAR,          -- 交易规则生效区间说明 (待 M3 完善)
    source                 VARCHAR,
    updated_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_DDL_UNIVERSE_VERSIONS = """
CREATE TABLE IF NOT EXISTS universe_versions (
    version_id  VARCHAR PRIMARY KEY,         -- 内容哈希版本
    strategy_id VARCHAR,
    as_of       DATE NOT NULL,               -- 声明生效日
    source      VARCHAR,                     -- settings / manual / research
    note        VARCHAR,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_DDL_UNIVERSE_MEMBERS = """
CREATE TABLE IF NOT EXISTS universe_members (
    version_id     VARCHAR NOT NULL,
    symbol         VARCHAR NOT NULL,
    effective_from DATE NOT NULL,
    effective_to   DATE,                     -- NULL = 当前仍有效
    include_reason VARCHAR,
    PRIMARY KEY (version_id, symbol)
)
"""

_DDL_NAV_DAILY = """
CREATE TABLE IF NOT EXISTS nav_daily (
    symbol     VARCHAR NOT NULL,
    date       DATE NOT NULL,
    nav        DOUBLE,
    source     VARCHAR,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, date)
)
"""

_DDL_STRATEGY_STATES = """
CREATE TABLE IF NOT EXISTS strategy_states (
    symbol         VARCHAR PRIMARY KEY,
    has_position   BOOLEAN DEFAULT FALSE,
    entry_date     DATE,                    -- 当前持有期起点 (批次最早 open_date)
    high_water     DOUBLE,                  -- 持有期最高收盘价 (只在持有期维护)
    atr            DOUBLE,                  -- 最新 ATR (Wilder, T 日收盘)
    stop_price     DOUBLE,                  -- 有效止损线 (棘轮, 只上移)
    stop_triggered BOOLEAN DEFAULT FALSE,   -- 本轮触发
    pending_exit   BOOLEAN DEFAULT FALSE,   -- 退出指令未完成 (持续展示风险)
    exit_reason    VARCHAR,
    exit_date      DATE,
    cooldown_until DATE,                    -- 重新进入冷却到期 (自然日近似)
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_ALL_DDL: list[tuple[str, str]] = [
    ("ohlcv_daily", _DDL_OHLCV),
    ("etf_metadata", _DDL_ETF_META),
    ("macro_indicators", _DDL_MACRO),
    ("trade_signals", _DDL_SIGNALS),
    ("account_state", _DDL_ACCOUNT),
    ("schema_version", _DDL_SCHEMA_VER),
    ("accounts", _DDL_ACCOUNTS),
    ("account_events", _DDL_ACCOUNT_EVENTS),
    ("position_lots", _DDL_POSITION_LOTS),
    ("account_snapshots", _DDL_ACCOUNT_SNAPSHOTS),
    ("targets", _DDL_TARGETS),
    ("target_weights", _DDL_TARGET_WEIGHTS),
    ("trade_proposals", _DDL_TRADE_PROPOSALS),
    ("execution_feedback", _DDL_EXECUTION_FEEDBACK),
    ("runs", _DDL_RUNS),
    ("experiments", _DDL_EXPERIMENTS),
    ("schema_migration_log", _DDL_SCHEMA_MIGRATION_LOG),
    ("instruments", _DDL_INSTRUMENTS),
    ("universe_versions", _DDL_UNIVERSE_VERSIONS),
    ("universe_members", _DDL_UNIVERSE_MEMBERS),
    ("nav_daily", _DDL_NAV_DAILY),
    ("strategy_states", _DDL_STRATEGY_STATES),
]


class DuckDBManager:
    """DuckDB 数据库管理器.

    Usage:
        db = DuckDBManager("data/quant_pilot.duckdb")
        db.init_database()
        db.upsert_ohlcv(df)
        result = db.get_ohlcv(symbols=["510300"])
        db.close()
    """

    def __init__(self, db_path: str) -> None:
        """初始化管理器.

        Args:
            db_path: .duckdb 文件路径 (相对或绝对)
        """
        self.db_path = Path(db_path)
        self._conn: Optional[duckdb.DuckDBPyConnection] = None

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        """惰性初始化连接."""
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = duckdb.connect(str(self.db_path))
            logger.debug("Connected to DuckDB: %s", self.db_path)
        return self._conn

    def close(self) -> None:
        """关闭数据库连接."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            logger.debug("DuckDB connection closed")

    @contextmanager
    def session(self) -> Generator[duckdb.DuckDBPyConnection, None, None]:
        """上下文管理器, 自动关闭连接."""
        try:
            yield self.conn
        finally:
            self.close()

    @contextmanager
    def transaction(self) -> Generator[duckdb.DuckDBPyConnection, None, None]:
        """显式事务: 异常时回滚 (不留半笔事件, plan §5 / spec §2)."""
        self.conn.execute("BEGIN TRANSACTION")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # 数据库初始化
    # ------------------------------------------------------------------

    def init_database(self) -> None:
        """创建所有表并执行幂等 schema 迁移.

        - CREATE TABLE IF NOT EXISTS 对全新/既有库均安全
        - 版本迁移只新增列/标注, 不改写既有事实 (plan §5)
        """
        logger.info("Initializing DuckDB schema (v%d)...", SCHEMA_VERSION)

        for table_name, ddl in _ALL_DDL:
            self.conn.execute(ddl)
            logger.debug("  Ensured table: %s", table_name)

        self._migrate_schema()
        logger.info("DuckDB schema ready")

    def _migrate_schema(self) -> None:
        """幂等版本迁移 (仅增列 / 标注旧记录, 不覆盖真实事实)."""
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version"
        ).fetchone()
        current = int(row[0]) if row and row[0] is not None else 0

        if current < 1:
            self.conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, description) VALUES (?, ?)",
                [1, "Initial schema v1: 6 tables for P0 MVP"],
            )

        if current < 2:
            # v2: trade_signals 增加策略版本列; 既有记录标注 v0.1 (1/σ² 旧口径, A0-1)
            self.conn.execute(
                "ALTER TABLE trade_signals ADD COLUMN IF NOT EXISTS strategy_version VARCHAR"
            )
            self.conn.execute(
                "UPDATE trade_signals SET strategy_version = 'v0.1' "
                "WHERE strategy_version IS NULL"
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, description) VALUES (?, ?)",
                [2, "v2: trade_signals.strategy_version; legacy rows marked v0.1"],
            )
            logger.info("Schema migrated to v2 (trade_signals.strategy_version)")

        if current < 3:
            # v3: 新表已由 _ALL_DDL 幂等创建 (只增表, 不改写既有事实);
            # 记录迁移与核对摘要 (DuckDB 不支持"失败半提交"的 DDL 回滚, 因此仅新增)
            checks = {
                t: self.get_table_count(t)
                for t in ("ohlcv_daily", "trade_signals")
            }
            self.conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, description) VALUES (?, ?)",
                [3, "v3: accounts/events/lots/snapshots + four-record tables + runs"],
            )
            self.conn.execute(
                "INSERT INTO schema_migration_log "
                "(version, description, checks_json, status) VALUES (?, ?, ?, ?)",
                [
                    3,
                    "v3: accounts/account_events/position_lots/account_snapshots/"
                    "targets/trade_proposals/execution_feedback/runs/experiments",
                    json.dumps(checks, ensure_ascii=False),
                    "applied",
                ],
            )
            logger.info("Schema migrated to v3 (%s)", checks)

        if current < 4:
            # v4: 产品契约与池版本表 (只增表; 供 M3 四层池使用)
            checks_v4 = {
                t: self.get_table_count(t)
                for t in ("ohlcv_daily", "trade_signals")
            }
            self.conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, description) VALUES (?, ?)",
                [4, "v4: instruments/universe_versions/universe_members/nav_daily"],
            )
            self.conn.execute(
                "INSERT INTO schema_migration_log "
                "(version, description, checks_json, status) VALUES (?, ?, ?, ?)",
                [
                    4,
                    "v4: instruments/universe_versions/universe_members/nav_daily",
                    json.dumps(checks_v4, ensure_ascii=False),
                    "applied",
                ],
            )
            logger.info("Schema migrated to v4 (%s)", checks_v4)

        if current < 5:
            # v5: 持有期状态表 (只增表; M4-06 状态化止损与冷却)
            checks_v5 = {
                t: self.get_table_count(t)
                for t in ("ohlcv_daily", "trade_signals", "account_events")
            }
            self.conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, description) VALUES (?, ?)",
                [5, "v5: strategy_states (持有期高水位/棘轮止损/冷却)"],
            )
            self.conn.execute(
                "INSERT INTO schema_migration_log "
                "(version, description, checks_json, status) VALUES (?, ?, ?, ?)",
                [
                    5,
                    "v5: strategy_states (M4-06 状态化 ATR 止损与重新进入冷却)",
                    json.dumps(checks_v5, ensure_ascii=False),
                    "applied",
                ],
            )
            logger.info("Schema migrated to v5 (%s)", checks_v5)

    # ------------------------------------------------------------------
    # OHLCV 写入 (UPSERT — 幂等)
    # ------------------------------------------------------------------

    def upsert_ohlcv(self, df: pl.DataFrame) -> int:
        """插入或更新日线 OHLCV 数据.

        以 (symbol, date) 为主键, 已存在的行更新 OHLCV 值,
        不存在的行插入新记录. 重复执行幂等.

        Args:
            df: Polars DataFrame
                必须包含: symbol, date, open, high, low, close
                可选: volume, amount

        Returns:
            实际写入行数

        Raises:
            ValueError: 缺少必需列
        """
        if df.is_empty():
            logger.warning("upsert_ohlcv: empty DataFrame, skipping")
            return 0

        self._validate_ohlcv_columns(df)

        # 注册为临时视图
        pdf = df.to_pandas()
        self.conn.register("_upsert_ohlcv", pdf)

        try:
            # DuckDB UPSERT: ON CONFLICT (symbol, date) DO UPDATE
            self.conn.execute("""
                INSERT INTO ohlcv_daily
                    (symbol, date, open, high, low, close, volume, amount)
                SELECT
                    symbol, date, open, high, low, close, volume, amount
                FROM _upsert_ohlcv
                ON CONFLICT (symbol, date) DO UPDATE SET
                    open      = EXCLUDED.open,
                    high      = EXCLUDED.high,
                    low       = EXCLUDED.low,
                    close     = EXCLUDED.close,
                    volume    = EXCLUDED.volume,
                    amount    = EXCLUDED.amount
            """)
        finally:
            self.conn.unregister("_upsert_ohlcv")

        row_count = len(df)
        logger.info("upsert_ohlcv: %d rows written", row_count)
        return row_count

    @staticmethod
    def _validate_ohlcv_columns(df: pl.DataFrame) -> None:
        """校验 OHLCV DataFrame 包含必要列."""
        required = {"symbol", "date", "open", "high", "low", "close"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(
                f"OHLCV DataFrame missing required columns: {missing}. "
                f"Got: {sorted(df.columns)}"
            )

    # ------------------------------------------------------------------
    # ETF 元数据写入
    # ------------------------------------------------------------------

    def upsert_etf_metadata(self, df: pl.DataFrame) -> int:
        """插入或更新 ETF 元数据.

        Args:
            df: 包含 symbol, name, market_type, settlement_type 等列

        Returns:
            写入行数
        """
        if df.is_empty():
            return 0

        pdf = df.to_pandas()
        self.conn.register("_upsert_meta", pdf)

        try:
            self.conn.execute("""
                INSERT INTO etf_metadata
                    (symbol, name, market_type, settlement_type, list_date,
                     avg_daily_volume_20d)
                SELECT
                    symbol, name, market_type, settlement_type, list_date,
                    avg_daily_volume_20d
                FROM _upsert_meta
                ON CONFLICT (symbol) DO UPDATE SET
                    name                 = EXCLUDED.name,
                    market_type          = EXCLUDED.market_type,
                    settlement_type      = EXCLUDED.settlement_type,
                    list_date            = COALESCE(EXCLUDED.list_date, etf_metadata.list_date),
                    avg_daily_volume_20d = COALESCE(EXCLUDED.avg_daily_volume_20d, etf_metadata.avg_daily_volume_20d)
            """)
        finally:
            self.conn.unregister("_upsert_meta")

        row_count = len(df)
        logger.info("upsert_etf_metadata: %d records", row_count)
        return row_count

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def get_ohlcv(
        self,
        symbols: Optional[list[str]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pl.DataFrame:
        """查询日线 OHLCV 数据.

        Args:
            symbols: 标的列表, None = 全部
            start_date: 起始日期 "YYYY-MM-DD", None = 不限
            end_date: 截止日期 "YYYY-MM-DD", None = 不限

        Returns:
            Polars DataFrame, 按 (symbol, date) 排序
        """
        query = "SELECT * FROM ohlcv_daily WHERE 1=1"
        params: list = []

        if symbols:
            placeholders = ", ".join(["?" for _ in symbols])
            query += f" AND symbol IN ({placeholders})"
            params.extend(symbols)

        if start_date:
            query += " AND date >= ?"
            params.append(start_date)

        if end_date:
            query += " AND date <= ?"
            params.append(end_date)

        query += " ORDER BY symbol, date"

        result = self.conn.execute(query, params).fetchall()
        if not result:
            return pl.DataFrame()

        columns = [desc[0] for desc in self.conn.description]
        return pl.DataFrame(result, schema=columns, orient="row")

    def upsert_signals(self, df: pl.DataFrame) -> int:
        """插入或更新交易信号 (幂等).

        以 signal_id 为主键 (调用方需生成确定性 ID,
        如 signal_id = 交易日期 YYYYMMDD * 1000 + 当日序号),
        重复执行相同日期的 pipeline 不产生重复行.

        Args:
            df: Polars DataFrame, 需包含:
                signal_id, signal_date, symbol, direction,
                target_weight, trigger_reason
                可选: strategy_version (缺省 NULL; 更新时 NULL 不覆盖已有版本)

        Returns:
            实际写入行数

        Raises:
            ValueError: 缺少必需列
        """
        if df.is_empty():
            logger.warning("upsert_signals: empty DataFrame, skipping")
            return 0

        required = {
            "signal_id", "signal_date", "symbol",
            "direction", "target_weight", "trigger_reason",
        }
        missing = required - set(df.columns)
        if missing:
            raise ValueError(
                f"Signals DataFrame missing required columns: {missing}. "
                f"Got: {sorted(df.columns)}"
            )

        payload = df
        if "strategy_version" not in payload.columns:
            payload = payload.with_columns(
                pl.lit(None, dtype=pl.Utf8).alias("strategy_version")
            )
        pdf = payload.to_pandas()
        self.conn.register("_upsert_signals", pdf)

        try:
            self.conn.execute("""
                INSERT INTO trade_signals
                    (signal_id, signal_date, symbol, direction,
                     target_weight, trigger_reason, strategy_version)
                SELECT
                    signal_id, signal_date, symbol, direction,
                    target_weight, trigger_reason, strategy_version
                FROM _upsert_signals
                ON CONFLICT (signal_id) DO UPDATE SET
                    signal_date      = EXCLUDED.signal_date,
                    symbol           = EXCLUDED.symbol,
                    direction        = EXCLUDED.direction,
                    target_weight    = EXCLUDED.target_weight,
                    trigger_reason   = EXCLUDED.trigger_reason,
                    strategy_version = COALESCE(
                        EXCLUDED.strategy_version,
                        trade_signals.strategy_version
                    )
            """)
        finally:
            self.conn.unregister("_upsert_signals")

        row_count = len(df)
        logger.info("upsert_signals: %d rows written", row_count)
        return row_count

    def get_latest_signals(
        self,
        before_date: Optional[str] = None,
    ) -> pl.DataFrame:
        """查询最近一个信号日的全部信号.

        Args:
            before_date: 若提供 (格式 "YYYY-MM-DD"),
                仅考虑该日期之前的信号日 (用于与昨日信号对比)

        Returns:
            Polars DataFrame, 列同 trade_signals 表; 无信号时返回空表
        """
        if not self.table_exists("trade_signals"):
            return pl.DataFrame()

        if before_date:
            row = self.conn.execute(
                "SELECT MAX(signal_date) FROM trade_signals WHERE signal_date < ?",
                [before_date],
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT MAX(signal_date) FROM trade_signals"
            ).fetchone()

        if row is None or row[0] is None:
            return pl.DataFrame()

        result = self.conn.execute(
            "SELECT * FROM trade_signals WHERE signal_date = ? ORDER BY symbol",
            [row[0]],
        ).fetchall()
        if not result:
            return pl.DataFrame()

        columns = [desc[0] for desc in self.conn.description]
        return pl.DataFrame(result, schema=columns, orient="row")

    def get_etf_metadata(
        self,
        symbols: Optional[list[str]] = None,
    ) -> pl.DataFrame:
        """查询 ETF 元数据."""
        if symbols:
            placeholders = ", ".join(["?" for _ in symbols])
            query = f"SELECT * FROM etf_metadata WHERE symbol IN ({placeholders})"
            params = list(symbols)
        else:
            query = "SELECT * FROM etf_metadata"
            params = []

        result = self.conn.execute(query, params).fetchall()
        if not result:
            return pl.DataFrame()

        columns = [desc[0] for desc in self.conn.description]
        return pl.DataFrame(result, schema=columns, orient="row")

    # ------------------------------------------------------------------
    # 账户账本 (v3 / M2): 账户 / 事件 / 批次 / 快照 / 运行记录
    # ------------------------------------------------------------------

    def upsert_account(self, df: pl.DataFrame) -> int:
        """插入或更新账户 (account_id 主键, mode=real/paper/backtest)."""
        if df.is_empty():
            return 0
        required = {"account_id", "mode"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"accounts DataFrame missing required columns: {missing}")

        payload = df
        for col, dtype in (("name", pl.Utf8), ("note", pl.Utf8)):
            if col not in payload.columns:
                payload = payload.with_columns(pl.lit(None, dtype=dtype).alias(col))
        if "base_currency" not in payload.columns:
            payload = payload.with_columns(pl.lit("CNY").alias("base_currency"))

        pdf = payload.to_pandas()
        self.conn.register("_upsert_account", pdf)
        try:
            self.conn.execute("""
                INSERT INTO accounts (account_id, name, mode, base_currency, note)
                SELECT account_id, name, mode,
                       COALESCE(base_currency, 'CNY'), note
                FROM _upsert_account
                ON CONFLICT (account_id) DO UPDATE SET
                    name          = EXCLUDED.name,
                    mode          = EXCLUDED.mode,
                    base_currency = EXCLUDED.base_currency,
                    note          = EXCLUDED.note
            """)
        finally:
            self.conn.unregister("_upsert_account")
        return len(df)

    def get_accounts(self) -> pl.DataFrame:
        """查询全部账户."""
        result = self.conn.execute(
            "SELECT * FROM accounts ORDER BY account_id"
        ).fetchall()
        if not result:
            return pl.DataFrame()
        columns = [desc[0] for desc in self.conn.description]
        return pl.DataFrame(result, schema=columns, orient="row")

    def insert_account_events(self, df: pl.DataFrame) -> tuple[int, int]:
        """批量插入账户事件 (event_id 稳定标识, 重复导入不产生第二笔).

        Returns:
            (inserted, skipped)
        """
        if df.is_empty():
            return 0, 0
        required = {"event_id", "account_id", "event_type", "event_time"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(
                f"events DataFrame missing required columns: {missing}. "
                f"Got: {sorted(df.columns)}"
            )

        payload = df
        for col, dtype in (
            ("symbol", pl.Utf8), ("side", pl.Utf8), ("shares", pl.Float64),
            ("price", pl.Float64), ("amount", pl.Float64), ("fee", pl.Float64),
            ("source", pl.Utf8), ("reference", pl.Utf8),
            ("correction_of", pl.Utf8), ("payload_json", pl.Utf8),
            ("note", pl.Utf8),
        ):
            if col not in payload.columns:
                payload = payload.with_columns(pl.lit(None, dtype=dtype).alias(col))

        before = self.get_table_count("account_events")
        pdf = payload.to_pandas()
        self.conn.register("_ins_events", pdf)
        try:
            self.conn.execute("""
                INSERT OR IGNORE INTO account_events
                    (event_id, account_id, event_type, event_time,
                     symbol, side, shares, price, amount, fee,
                     source, reference, correction_of, payload_json, note)
                SELECT
                    event_id, account_id, event_type, event_time,
                    symbol, side, shares, price, amount, fee,
                    source, reference, correction_of, payload_json, note
                FROM _ins_events
            """)
        finally:
            self.conn.unregister("_ins_events")
        inserted = self.get_table_count("account_events") - before
        return inserted, len(df) - inserted

    def get_account_events(self, account_id: str) -> pl.DataFrame:
        """查询账户全部事件 (按时间排序, 供回放)."""
        result = self.conn.execute(
            "SELECT * FROM account_events WHERE account_id = ? "
            "ORDER BY event_time, event_id",
            [account_id],
        ).fetchall()
        if not result:
            return pl.DataFrame()
        columns = [desc[0] for desc in self.conn.description]
        return pl.DataFrame(result, schema=columns, orient="row")

    def replace_position_lots(self, account_id: str, df: pl.DataFrame) -> int:
        """重建某账户的批次缓存 (派生数据; 权威来源 = 事件回放)."""
        self.conn.execute(
            "DELETE FROM position_lots WHERE account_id = ?", [account_id],
        )
        if df.is_empty():
            return 0
        pdf = df.to_pandas()
        self.conn.register("_pos_lots", pdf)
        try:
            self.conn.execute("""
                INSERT INTO position_lots
                    (lot_id, account_id, symbol, open_event_id, shares,
                     cost_basis, open_date, sellable_date, closed)
                SELECT lot_id, account_id, symbol, open_event_id, shares,
                       cost_basis, open_date, sellable_date, closed
                FROM _pos_lots
            """)
        finally:
            self.conn.unregister("_pos_lots")
        return len(df)

    def get_position_lots(self, account_id: str) -> pl.DataFrame:
        """查询账户当前批次缓存."""
        result = self.conn.execute(
            "SELECT * FROM position_lots WHERE account_id = ? "
            "ORDER BY symbol, open_date",
            [account_id],
        ).fetchall()
        if not result:
            return pl.DataFrame()
        columns = [desc[0] for desc in self.conn.description]
        return pl.DataFrame(result, schema=columns, orient="row")

    def get_position_entry_dates(self, account_id: str) -> dict[str, date]:
        """账户各标的持有期起点 = 未平批次最早 open_date (M4-06 高水位用)."""
        result = self.conn.execute(
            "SELECT symbol, MIN(open_date) AS entry_date FROM position_lots "
            "WHERE account_id = ? GROUP BY symbol ORDER BY symbol",
            [account_id],
        ).fetchall()
        return {
            str(symbol): entry_date
            for symbol, entry_date in result
            if entry_date is not None
        }

    def upsert_account_snapshot(self, df: pl.DataFrame) -> int:
        """写入账户快照 (account_id + as_of 主键)."""
        if df.is_empty():
            return 0
        required = {"account_id", "as_of"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"snapshot DataFrame missing required columns: {missing}")

        payload = df
        for col, dtype in (
            ("cash", pl.Float64), ("cash_withdrawable", pl.Float64),
            ("cash_reserved", pl.Float64), ("receivable", pl.Float64),
            ("positions_json", pl.Utf8), ("event_cutoff", pl.Utf8),
            ("reconciled", pl.Boolean), ("note", pl.Utf8),
        ):
            if col not in payload.columns:
                payload = payload.with_columns(pl.lit(None, dtype=dtype).alias(col))

        pdf = payload.to_pandas()
        self.conn.register("_acc_snap", pdf)
        try:
            self.conn.execute("""
                INSERT INTO account_snapshots
                    (account_id, as_of, cash, cash_withdrawable, cash_reserved,
                     receivable, positions_json, event_cutoff, reconciled, note)
                SELECT account_id, as_of, cash, cash_withdrawable, cash_reserved,
                       receivable, positions_json, event_cutoff,
                       COALESCE(reconciled, FALSE), note
                FROM _acc_snap
                ON CONFLICT (account_id, as_of) DO UPDATE SET
                    cash              = EXCLUDED.cash,
                    cash_withdrawable = EXCLUDED.cash_withdrawable,
                    cash_reserved     = EXCLUDED.cash_reserved,
                    receivable        = EXCLUDED.receivable,
                    positions_json    = EXCLUDED.positions_json,
                    event_cutoff      = EXCLUDED.event_cutoff,
                    reconciled        = EXCLUDED.reconciled,
                    note              = EXCLUDED.note
            """)
        finally:
            self.conn.unregister("_acc_snap")
        return len(df)

    def get_latest_account_snapshot(self, account_id: str) -> dict | None:
        """最近一次账户快照 (dict; 无则 None)."""
        row = self.conn.execute(
            "SELECT * FROM account_snapshots WHERE account_id = ? "
            "ORDER BY as_of DESC LIMIT 1",
            [account_id],
        ).fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row, strict=True))

    def insert_run(self, df: pl.DataFrame) -> int:
        """写入运行记录 (run_id 主键, 重跑更新同一 run_id)."""
        if df.is_empty():
            return 0
        required = {"run_id", "run_type", "status"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"runs DataFrame missing required columns: {missing}")

        payload = df
        for col, dtype in (
            ("run_date", pl.Date), ("mode", pl.Utf8),
            ("strategy_version", pl.Utf8), ("data_version", pl.Utf8),
            ("push_ok", pl.Boolean), ("push_attempts", pl.Int64),
            ("push_error", pl.Utf8), ("detail_json", pl.Utf8),
            ("started_at", pl.Datetime), ("finished_at", pl.Datetime),
        ):
            if col not in payload.columns:
                payload = payload.with_columns(pl.lit(None, dtype=dtype).alias(col))

        pdf = payload.to_pandas()
        self.conn.register("_runs", pdf)
        try:
            self.conn.execute("""
                INSERT INTO runs
                    (run_id, run_type, run_date, mode, strategy_version,
                     data_version, status, push_ok, push_attempts, push_error,
                     detail_json, started_at, finished_at)
                SELECT run_id, run_type, run_date, mode, strategy_version,
                       data_version, status, push_ok, push_attempts, push_error,
                       detail_json, started_at, finished_at
                FROM _runs
                ON CONFLICT (run_id) DO UPDATE SET
                    status         = EXCLUDED.status,
                    data_version   = EXCLUDED.data_version,
                    push_ok        = EXCLUDED.push_ok,
                    push_attempts  = EXCLUDED.push_attempts,
                    push_error     = EXCLUDED.push_error,
                    detail_json    = EXCLUDED.detail_json,
                    finished_at    = EXCLUDED.finished_at
            """)
        finally:
            self.conn.unregister("_runs")
        return len(df)

    def update_run_push(
        self,
        run_id: str,
        ok: bool,
        attempts: int,
        error: str | None = None,
    ) -> None:
        """更新运行记录的推送结果 (失败状态可追踪, A0-2)."""
        self.conn.execute(
            "UPDATE runs SET push_ok = ?, push_attempts = ?, push_error = ? "
            "WHERE run_id = ?",
            [ok, attempts, error, run_id],
        )

    def get_run(self, run_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", [run_id],
        ).fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row, strict=True))

    # ------------------------------------------------------------------
    # 实验记录 (v3 / M5: 规则实验追踪)
    # ------------------------------------------------------------------

    def upsert_experiment(self, df: pl.DataFrame) -> int:
        """写入实验记录 (experiment_id 主键; 失败试验同样保留, spec §7.1)."""
        if df.is_empty():
            return 0
        required = {"experiment_id", "kind", "status"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"experiments DataFrame missing required columns: {missing}")
        payload = df
        for col, dtype in (
            ("code_version", pl.Utf8), ("data_version", pl.Utf8),
            ("pool_version", pl.Utf8), ("config_json", pl.Utf8),
            ("cost_scenario", pl.Utf8), ("purpose", pl.Utf8),
            ("result_location", pl.Utf8), ("conclusion", pl.Utf8),
        ):
            if col not in payload.columns:
                payload = payload.with_columns(pl.lit(None, dtype=dtype).alias(col))
        pdf = payload.to_pandas()
        self.conn.register("_experiments", pdf)
        try:
            self.conn.execute("""
                INSERT INTO experiments
                    (experiment_id, kind, code_version, data_version,
                     pool_version, config_json, cost_scenario, purpose,
                     result_location, status, conclusion)
                SELECT experiment_id, kind, code_version, data_version,
                       pool_version, config_json, cost_scenario, purpose,
                       result_location, status, conclusion
                FROM _experiments
                ON CONFLICT (experiment_id) DO UPDATE SET
                    code_version    = EXCLUDED.code_version,
                    data_version    = EXCLUDED.data_version,
                    pool_version    = EXCLUDED.pool_version,
                    config_json     = EXCLUDED.config_json,
                    cost_scenario   = EXCLUDED.cost_scenario,
                    purpose         = EXCLUDED.purpose,
                    result_location = EXCLUDED.result_location,
                    status          = EXCLUDED.status,
                    conclusion      = EXCLUDED.conclusion
            """)
        finally:
            self.conn.unregister("_experiments")
        return len(df)

    def get_experiment(self, experiment_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM experiments WHERE experiment_id = ?", [experiment_id],
        ).fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row, strict=True))

    # ------------------------------------------------------------------
    # 目标 / 建议 / 反馈 (v3 / M2b: 四类记录其余三层)
    # ------------------------------------------------------------------

    def upsert_target(self, df: pl.DataFrame) -> int:
        """写入策略目标 (target_id 主键, 重跑更新)."""
        if df.is_empty():
            return 0
        required = {"target_id", "strategy_id", "mode", "as_of"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"targets DataFrame missing required columns: {missing}")

        payload = df
        for col, dtype in (
            ("strategy_version", pl.Utf8), ("decision_time", pl.Datetime),
            ("data_version", pl.Utf8), ("pool_version", pl.Utf8),
            ("config_version", pl.Utf8), ("reason", pl.Utf8),
        ):
            if col not in payload.columns:
                payload = payload.with_columns(pl.lit(None, dtype=dtype).alias(col))

        pdf = payload.to_pandas()
        self.conn.register("_targets", pdf)
        try:
            self.conn.execute("""
                INSERT INTO targets
                    (target_id, strategy_id, strategy_version, mode, as_of,
                     decision_time, data_version, pool_version,
                     config_version, reason)
                SELECT target_id, strategy_id, strategy_version, mode, as_of,
                       decision_time, data_version, pool_version,
                       config_version, reason
                FROM _targets
                ON CONFLICT (target_id) DO UPDATE SET
                    strategy_version = EXCLUDED.strategy_version,
                    data_version     = EXCLUDED.data_version,
                    pool_version     = EXCLUDED.pool_version,
                    config_version   = EXCLUDED.config_version,
                    reason           = EXCLUDED.reason
            """)
        finally:
            self.conn.unregister("_targets")
        return len(df)

    def replace_target_weights(self, target_id: str, df: pl.DataFrame) -> int:
        """重建某目标的完整权重集 (含现金权重行 __CASH__)."""
        self.conn.execute(
            "DELETE FROM target_weights WHERE target_id = ?", [target_id],
        )
        if df.is_empty():
            return 0
        pdf = df.to_pandas()
        self.conn.register("_tw", pdf)
        try:
            self.conn.execute("""
                INSERT INTO target_weights (target_id, symbol, target_weight)
                SELECT target_id, symbol, target_weight FROM _tw
            """)
        finally:
            self.conn.unregister("_tw")
        return len(df)

    def get_target(self, target_id: str) -> dict | None:
        """按 target_id 读取目标."""
        row = self.conn.execute(
            "SELECT * FROM targets WHERE target_id = ?", [target_id],
        ).fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row, strict=True))

    def get_target_weights(self, target_id: str) -> pl.DataFrame:
        """读取目标完整权重 (按权重降序)."""
        result = self.conn.execute(
            "SELECT symbol, target_weight FROM target_weights "
            "WHERE target_id = ? ORDER BY target_weight DESC, symbol",
            [target_id],
        ).fetchall()
        if not result:
            return pl.DataFrame()
        return pl.DataFrame(
            result, schema=["symbol", "target_weight"], orient="row",
        )

    def insert_proposals(self, df: pl.DataFrame) -> tuple[int, int]:
        """批量插入交易建议 (proposal_id 去重). Returns: (inserted, skipped)."""
        if df.is_empty():
            return 0, 0
        required = {"proposal_id", "symbol", "side", "shares"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"proposals DataFrame missing required columns: {missing}")

        payload = df
        for col, dtype in (
            ("target_id", pl.Utf8), ("account_id", pl.Utf8),
            ("account_as_of", pl.Datetime), ("ref_price", pl.Float64),
            ("ref_time", pl.Datetime), ("est_amount", pl.Float64),
            ("est_fee", pl.Float64), ("target_weight", pl.Float64),
            ("actual_weight", pl.Float64), ("valid_until", pl.Datetime),
            ("priority", pl.Int64), ("depends_on", pl.Utf8),
            ("status", pl.Utf8), ("reason", pl.Utf8),
        ):
            if col not in payload.columns:
                payload = payload.with_columns(pl.lit(None, dtype=dtype).alias(col))

        before = self.get_table_count("trade_proposals")
        pdf = payload.to_pandas()
        self.conn.register("_props", pdf)
        try:
            self.conn.execute("""
                INSERT OR IGNORE INTO trade_proposals
                    (proposal_id, target_id, account_id, account_as_of,
                     symbol, side, shares, ref_price, ref_time,
                     est_amount, est_fee, target_weight, actual_weight,
                     valid_until, priority, depends_on, status, reason)
                SELECT proposal_id, target_id, account_id, account_as_of,
                       symbol, side, shares, ref_price, ref_time,
                       est_amount, est_fee, target_weight, actual_weight,
                       valid_until, priority, depends_on,
                       COALESCE(status, 'proposed'), reason
                FROM _props
            """)
        finally:
            self.conn.unregister("_props")
        inserted = self.get_table_count("trade_proposals") - before
        return inserted, len(df) - inserted

    def get_proposal(self, proposal_id: str) -> dict | None:
        """按 proposal_id 读取建议."""
        row = self.conn.execute(
            "SELECT * FROM trade_proposals WHERE proposal_id = ?", [proposal_id],
        ).fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row, strict=True))

    def update_proposal_status(self, proposal_id: str, status: str) -> None:
        """更新建议状态 (proposed/partial/filled/expired/replaced/rejected)."""
        self.conn.execute(
            "UPDATE trade_proposals SET status = ? WHERE proposal_id = ?",
            [status, proposal_id],
        )

    def get_open_proposals(self, account_id: str | None = None) -> pl.DataFrame:
        """读取开放建议 (proposed / partially_filled)."""
        query = (
            "SELECT * FROM trade_proposals "
            "WHERE status IN ('proposed', 'partially_filled')"
        )
        params: list = []
        if account_id:
            query += " AND account_id = ?"
            params.append(account_id)
        query += " ORDER BY priority DESC, proposal_id"
        result = self.conn.execute(query, params).fetchall()
        if not result:
            return pl.DataFrame()
        columns = [desc[0] for desc in self.conn.description]
        return pl.DataFrame(result, schema=columns, orient="row")

    def insert_feedback(self, df: pl.DataFrame) -> tuple[int, int]:
        """批量插入执行反馈 (feedback_id 去重). Returns: (inserted, skipped)."""
        if df.is_empty():
            return 0, 0
        required = {"feedback_id", "feedback_time", "status"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"feedback DataFrame missing required columns: {missing}")

        payload = df
        for col, dtype in (
            ("proposal_id", pl.Utf8), ("account_id", pl.Utf8),
            ("trade_id", pl.Utf8), ("shares", pl.Float64),
            ("price", pl.Float64), ("fee", pl.Float64),
            ("source", pl.Utf8), ("reason", pl.Utf8),
        ):
            if col not in payload.columns:
                payload = payload.with_columns(pl.lit(None, dtype=dtype).alias(col))

        before = self.get_table_count("execution_feedback")
        pdf = payload.to_pandas()
        self.conn.register("_fbs", pdf)
        try:
            self.conn.execute("""
                INSERT OR IGNORE INTO execution_feedback
                    (feedback_id, proposal_id, account_id, trade_id,
                     feedback_time, status, shares, price, fee, source, reason)
                SELECT feedback_id, proposal_id, account_id, trade_id,
                       feedback_time, status, shares, price, fee, source, reason
                FROM _fbs
            """)
        finally:
            self.conn.unregister("_fbs")
        inserted = self.get_table_count("execution_feedback") - before
        return inserted, len(df) - inserted

    def get_feedback_for_proposal(self, proposal_id: str) -> pl.DataFrame:
        """读取某建议的全部执行反馈."""
        result = self.conn.execute(
            "SELECT * FROM execution_feedback WHERE proposal_id = ? "
            "ORDER BY feedback_time, feedback_id",
            [proposal_id],
        ).fetchall()
        if not result:
            return pl.DataFrame()
        columns = [desc[0] for desc in self.conn.description]
        return pl.DataFrame(result, schema=columns, orient="row")

    def mark_targets_replaced(self, new_target_id: str) -> int:
        """新目标生效后, 引用旧目标且仍开放的建议标记 replaced (spec §3.2).

        Returns: 被替换的建议数.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) FROM trade_proposals "
            "WHERE status IN ('proposed', 'partially_filled') "
            "AND target_id IS NOT NULL AND target_id != ?",
            [new_target_id],
        ).fetchone()
        count = int(row[0]) if row else 0
        if count:
            self.conn.execute(
                "UPDATE trade_proposals SET status = 'replaced' "
                "WHERE status IN ('proposed', 'partially_filled') "
                "AND target_id IS NOT NULL AND target_id != ?",
                [new_target_id],
            )
        return count

    def mark_open_replaced_except(self, current_target_id: str | None) -> int:
        """新计划生成后, 非当前目标 (或全部) 的开放建议标记 replaced.

        current_target_id=None 时替代全部开放建议 (新计划已完整重算).
        Returns: 被替换的建议数.
        """
        if current_target_id is None:
            count_row = self.conn.execute(
                "SELECT COUNT(*) FROM trade_proposals "
                "WHERE status IN ('proposed', 'partially_filled')"
            ).fetchone()
            count = int(count_row[0]) if count_row else 0
            if count:
                self.conn.execute(
                    "UPDATE trade_proposals SET status = 'replaced' "
                    "WHERE status IN ('proposed', 'partially_filled')"
                )
            return count
        return self.mark_targets_replaced(current_target_id)

    # ------------------------------------------------------------------
    # 产品契约 / 池版本 / NAV (v4 / M3)
    # ------------------------------------------------------------------

    def upsert_instruments(self, df: pl.DataFrame) -> int:
        """写入产品契约 (symbol 主键)."""
        if df.is_empty():
            return 0
        required = {"symbol"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"instruments DataFrame missing required columns: {missing}")

        payload = df
        for col, dtype in (
            ("name", pl.Utf8), ("exchange", pl.Utf8), ("market_type", pl.Utf8),
            ("asset_class", pl.Utf8), ("index_name", pl.Utf8),
            ("fund_type", pl.Utf8), ("currency", pl.Utf8),
            ("underlying_market", pl.Utf8), ("list_date", pl.Date),
            ("delist_date", pl.Date), ("settlement_type", pl.Utf8),
            ("price_limit_pct", pl.Float64), ("premium_check_required", pl.Boolean),
            ("rule_note", pl.Utf8), ("source", pl.Utf8),
        ):
            if col not in payload.columns:
                payload = payload.with_columns(pl.lit(None, dtype=dtype).alias(col))

        pdf = payload.to_pandas()
        self.conn.register("_instr", pdf)
        try:
            self.conn.execute("""
                INSERT INTO instruments
                    (symbol, name, exchange, market_type, asset_class, index_name,
                     fund_type, currency, underlying_market, list_date, delist_date,
                     settlement_type, price_limit_pct, premium_check_required,
                     rule_note, source)
                SELECT symbol, name, exchange, market_type, asset_class, index_name,
                       COALESCE(fund_type, 'ETF'), COALESCE(currency, 'CNY'),
                       underlying_market, list_date, delist_date,
                       settlement_type, price_limit_pct,
                       COALESCE(premium_check_required, FALSE),
                       rule_note, source
                FROM _instr
                ON CONFLICT (symbol) DO UPDATE SET
                    name                   = EXCLUDED.name,
                    exchange               = EXCLUDED.exchange,
                    market_type            = EXCLUDED.market_type,
                    asset_class            = EXCLUDED.asset_class,
                    index_name             = EXCLUDED.index_name,
                    underlying_market      = EXCLUDED.underlying_market,
                    list_date              = COALESCE(EXCLUDED.list_date, instruments.list_date),
                    settlement_type        = EXCLUDED.settlement_type,
                    price_limit_pct        = EXCLUDED.price_limit_pct,
                    premium_check_required = EXCLUDED.premium_check_required,
                    rule_note              = EXCLUDED.rule_note,
                    source                 = EXCLUDED.source,
                    updated_at             = now()
            """)
        finally:
            self.conn.unregister("_instr")
        return len(df)

    def get_instruments(self, symbols: list[str] | None = None) -> pl.DataFrame:
        """查询产品契约."""
        if symbols:
            placeholders = ", ".join(["?" for _ in symbols])
            result = self.conn.execute(
                f"SELECT * FROM instruments WHERE symbol IN ({placeholders}) "
                "ORDER BY symbol",
                list(symbols),
            ).fetchall()
        else:
            result = self.conn.execute(
                "SELECT * FROM instruments ORDER BY symbol"
            ).fetchall()
        if not result:
            return pl.DataFrame()
        columns = [desc[0] for desc in self.conn.description]
        return pl.DataFrame(result, schema=columns, orient="row")

    def get_symbol_date_ranges(self) -> pl.DataFrame:
        """各标的数据区间 (symbol, first_date, last_date); 用于上市近似与停牌判断."""
        result = self.conn.execute(
            "SELECT symbol, MIN(date) AS first_date, MAX(date) AS last_date "
            "FROM ohlcv_daily GROUP BY symbol ORDER BY symbol"
        ).fetchall()
        if not result:
            return pl.DataFrame()
        return pl.DataFrame(
            result, schema=["symbol", "first_date", "last_date"], orient="row",
        )

    def upsert_universe_version(self, df: pl.DataFrame) -> int:
        """写入池版本 (version_id 内容哈希; 已存在则忽略)."""
        if df.is_empty():
            return 0
        required = {"version_id", "as_of"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"universes DataFrame missing required columns: {missing}")
        payload = df
        for col, dtype in (
            ("strategy_id", pl.Utf8), ("source", pl.Utf8), ("note", pl.Utf8),
        ):
            if col not in payload.columns:
                payload = payload.with_columns(pl.lit(None, dtype=dtype).alias(col))
        before = self.get_table_count("universe_versions")
        pdf = payload.to_pandas()
        self.conn.register("_uv", pdf)
        try:
            self.conn.execute("""
                INSERT OR IGNORE INTO universe_versions
                    (version_id, strategy_id, as_of, source, note)
                SELECT version_id, strategy_id, as_of, source, note FROM _uv
            """)
        finally:
            self.conn.unregister("_uv")
        return self.get_table_count("universe_versions") - before

    def get_universe_version(self, version_id: str) -> dict | None:
        """按 version_id 读取池版本."""
        row = self.conn.execute(
            "SELECT * FROM universe_versions WHERE version_id = ?", [version_id],
        ).fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row, strict=True))

    def get_latest_universe_version(self) -> dict | None:
        """最近声明的池版本 (as_of 最新)."""
        row = self.conn.execute(
            "SELECT * FROM universe_versions ORDER BY as_of DESC, created_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row, strict=True))

    def insert_universe_members(self, df: pl.DataFrame) -> int:
        """写入池成员 (version_id+symbol 去重)."""
        if df.is_empty():
            return 0
        required = {"version_id", "symbol", "effective_from"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"members DataFrame missing required columns: {missing}")
        payload = df
        for col, dtype in (("effective_to", pl.Date), ("include_reason", pl.Utf8)):
            if col not in payload.columns:
                payload = payload.with_columns(pl.lit(None, dtype=dtype).alias(col))
        before = self.get_table_count("universe_members")
        pdf = payload.to_pandas()
        self.conn.register("_um", pdf)
        try:
            self.conn.execute("""
                INSERT OR IGNORE INTO universe_members
                    (version_id, symbol, effective_from, effective_to, include_reason)
                SELECT version_id, symbol, effective_from, effective_to, include_reason
                FROM _um
            """)
        finally:
            self.conn.unregister("_um")
        return self.get_table_count("universe_members") - before

    def get_universe_members(self, version_id: str) -> pl.DataFrame:
        """读取某版本池成员."""
        result = self.conn.execute(
            "SELECT * FROM universe_members WHERE version_id = ? ORDER BY symbol",
            [version_id],
        ).fetchall()
        if not result:
            return pl.DataFrame()
        columns = [desc[0] for desc in self.conn.description]
        return pl.DataFrame(result, schema=columns, orient="row")

    def get_open_universe_members(self) -> pl.DataFrame:
        """读取当前仍有效的池成员 (effective_to IS NULL)."""
        result = self.conn.execute(
            "SELECT * FROM universe_members WHERE effective_to IS NULL ORDER BY symbol"
        ).fetchall()
        if not result:
            return pl.DataFrame()
        columns = [desc[0] for desc in self.conn.description]
        return pl.DataFrame(result, schema=columns, orient="row")

    def close_universe_member(self, symbol: str, effective_to: date) -> None:
        """成员出池: 标注生效区间终点 (保留历史, 不删除)."""
        self.conn.execute(
            "UPDATE universe_members SET effective_to = ? "
            "WHERE symbol = ? AND effective_to IS NULL",
            [effective_to, symbol],
        )

    def upsert_nav_daily(self, df: pl.DataFrame) -> int:
        """写入 NAV/IOPV 日度数据 (symbol+date 主键, 幂等)."""
        if df.is_empty():
            return 0
        required = {"symbol", "date", "nav"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"nav DataFrame missing required columns: {missing}")
        payload = df
        if "source" not in payload.columns:
            payload = payload.with_columns(pl.lit(None, dtype=pl.Utf8).alias("source"))
        pdf = payload.to_pandas()
        self.conn.register("_nav", pdf)
        try:
            self.conn.execute("""
                INSERT INTO nav_daily (symbol, date, nav, source)
                SELECT symbol, date, nav, source FROM _nav
                ON CONFLICT (symbol, date) DO UPDATE SET
                    nav = EXCLUDED.nav, source = EXCLUDED.source,
                    updated_at = now()
            """)
        finally:
            self.conn.unregister("_nav")
        return len(df)

    def get_nav_daily(self, symbols: list[str] | None = None) -> pl.DataFrame:
        """查询 NAV 数据 (空表 = 数据未接入, 由调用方按 unknown 处理)."""
        if symbols:
            placeholders = ", ".join(["?" for _ in symbols])
            result = self.conn.execute(
                f"SELECT * FROM nav_daily WHERE symbol IN ({placeholders}) "
                "ORDER BY symbol, date",
                list(symbols),
            ).fetchall()
        else:
            result = self.conn.execute(
                "SELECT * FROM nav_daily ORDER BY symbol, date"
            ).fetchall()
        if not result:
            return pl.DataFrame()
        columns = [desc[0] for desc in self.conn.description]
        return pl.DataFrame(result, schema=columns, orient="row")

    # ------------------------------------------------------------------
    # 策略状态 (持有期高水位/棘轮止损/冷却; M4-06)
    # ------------------------------------------------------------------

    def upsert_strategy_states(self, df: pl.DataFrame) -> int:
        """写入策略状态 (symbol 主键 UPSERT, 幂等)."""
        if df.is_empty():
            return 0
        required = {"symbol"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"strategy_states DataFrame missing columns: {missing}")
        payload = df
        for col, dtype in (
            ("has_position", pl.Boolean), ("entry_date", pl.Date),
            ("high_water", pl.Float64), ("atr", pl.Float64),
            ("stop_price", pl.Float64), ("stop_triggered", pl.Boolean),
            ("pending_exit", pl.Boolean), ("exit_reason", pl.Utf8),
            ("exit_date", pl.Date), ("cooldown_until", pl.Date),
        ):
            if col not in payload.columns:
                payload = payload.with_columns(pl.lit(None, dtype=dtype).alias(col))
        pdf = payload.to_pandas()
        self.conn.register("_strategy_states", pdf)
        try:
            self.conn.execute("""
                INSERT INTO strategy_states
                    (symbol, has_position, entry_date, high_water, atr,
                     stop_price, stop_triggered, pending_exit, exit_reason,
                     exit_date, cooldown_until)
                SELECT symbol,
                       COALESCE(has_position, FALSE),
                       entry_date, high_water, atr, stop_price,
                       COALESCE(stop_triggered, FALSE),
                       COALESCE(pending_exit, FALSE),
                       exit_reason, exit_date, cooldown_until
                FROM _strategy_states
                ON CONFLICT (symbol) DO UPDATE SET
                    has_position   = EXCLUDED.has_position,
                    entry_date     = EXCLUDED.entry_date,
                    high_water     = EXCLUDED.high_water,
                    atr            = EXCLUDED.atr,
                    stop_price     = EXCLUDED.stop_price,
                    stop_triggered = EXCLUDED.stop_triggered,
                    pending_exit   = EXCLUDED.pending_exit,
                    exit_reason    = EXCLUDED.exit_reason,
                    exit_date      = EXCLUDED.exit_date,
                    cooldown_until = EXCLUDED.cooldown_until,
                    updated_at     = now()
            """)
        finally:
            self.conn.unregister("_strategy_states")
        return len(df)

    def get_strategy_states(self, symbols: list[str] | None = None) -> pl.DataFrame:
        """读取策略状态 (按标的过滤可选)."""
        if not self.table_exists("strategy_states"):
            return pl.DataFrame()
        if symbols:
            placeholders = ", ".join(["?" for _ in symbols])
            result = self.conn.execute(
                f"SELECT * FROM strategy_states WHERE symbol IN ({placeholders}) "
                "ORDER BY symbol",
                list(symbols),
            ).fetchall()
        else:
            result = self.conn.execute(
                "SELECT * FROM strategy_states ORDER BY symbol"
            ).fetchall()
        if not result:
            return pl.DataFrame()
        columns = [desc[0] for desc in self.conn.description]
        return pl.DataFrame(result, schema=columns, orient="row")

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def get_table_count(self, table_name: str) -> int:
        """获取表行数."""
        result = self.conn.execute(
            f"SELECT COUNT(*) FROM {table_name}"
        ).fetchone()
        return result[0] if result else 0

    def table_exists(self, table_name: str) -> bool:
        """检查表是否存在."""
        result = self.conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name = ? AND table_schema = 'main'",
            [table_name],
        ).fetchone()
        return result[0] > 0 if result else False

    def get_date_range(self) -> tuple[Optional[str], Optional[str]]:
        """获取 ohlcv_daily 表的日期范围."""
        if not self.table_exists("ohlcv_daily"):
            return None, None

        result = self.conn.execute(
            "SELECT MIN(date), MAX(date) FROM ohlcv_daily"
        ).fetchone()
        return (result[0], result[1]) if result else (None, None)

    def get_symbols(self) -> list[str]:
        """获取数据库中所有标的代码."""
        if not self.table_exists("ohlcv_daily"):
            return []

        result = self.conn.execute(
            "SELECT DISTINCT symbol FROM ohlcv_daily ORDER BY symbol"
        ).fetchall()
        return [r[0] for r in result]


# ------------------------------------------------------------------
# CLI 入口 (uv run init-db)
# ------------------------------------------------------------------

def main() -> None:
    """CLI: 初始化数据库 schema."""
    import sys
    from pathlib import Path

    import yaml

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    project_root = Path(__file__).resolve().parents[2]
    settings_path = project_root / "config" / "settings.yaml"

    if not settings_path.exists():
        logger.error("settings.yaml not found at %s", settings_path)
        sys.exit(1)

    with open(settings_path, encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    db_path = project_root / settings["storage"]["db_path"]
    db = DuckDBManager(str(db_path))
    db.init_database()
    logger.info("Database initialized at %s", db_path)
    db.close()


if __name__ == "__main__":
    main()
