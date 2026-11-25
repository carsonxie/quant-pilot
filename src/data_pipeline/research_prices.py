"""版本化研究价格序列 (Phase 1, P1-M1-02).

背景: Sina ETF 行情为不复权价; 已核验的拆分/合并事件会造成价格跳变,
分红则造成除息缺口。研究因子(动量/波动率)必须在连续的总收益序列上计算,
但原始 `ohlcv_daily` 不得改写。

本模块输出 (每标的):
  - `adj_open/high/low/close`: 按**拆分/合并比例**后复权的价格 (乘累计因子);
    量与额保持原始口径 (不调整)。
  - `tr_index`: 总收益指数 (首日=100), 事件日收益按:
      拆分/合并: (close_t × ratio) / close_{t-1} − 1
      分红:      (close_t + cash_per_share) / close_{t-1} − 1
  - `quality_flag`: verified_event / normal / unverified_jump / first。
  - `version`: `rp:v1:<hash>` (事件集 + 数据截止 + 代码版本; 确定性)。

口径声明:
  - `tr_index` 是**总收益**序列, 不是可成交价格; 组合回测/账本仍用原始价。
  - `adj_*` 仅做拆分/合并复权, 不含分红; 分红只进入 `tr_index`。
  - 未核验事件不调整; 若原始收益超过阈值且无已核验事件 → `unverified_jump` 标记。
  - 事件比例语义为 price_factor (后一收盘 ≈ 前一收盘 / ratio; 1:2 拆分 → 2.0)。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date, datetime
from pathlib import Path

import duckdb
import polars as pl

SPLIT_TYPES = {"split_official", "merge_official", "split_implied", "merge_implied"}
DIVIDEND_TYPES = {"dividend_official", "dividend_implied"}
DEFAULT_JUMP_THRESHOLD = 0.15
VERSION_PREFIX = "rp:v1"


def parse_cash_per_share(method: str | None) -> float | None:
    """从 `equity_events.method` JSON 读取每股现金分红 (元/份)."""
    if not method:
        return None
    try:
        payload = json.loads(method)
    except (TypeError, ValueError):
        return None
    value = payload.get("cash_per_share")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def prepare_events(events: pl.DataFrame) -> dict[str, dict[date, dict]]:
    """按 symbol → event_date 建立事件索引; 解析分红现金与事件类别."""
    result: dict[str, dict[date, dict]] = {}
    if events.is_empty():
        return result
    for row in events.iter_rows(named=True):
        symbol = str(row["symbol"]).zfill(6)
        event_date = row["event_date"]
        cash = parse_cash_per_share(row.get("method"))
        event_type = str(row.get("event_type") or "")
        if event_type in DIVIDEND_TYPES:
            kind = "dividend"
        elif event_type in SPLIT_TYPES:
            kind = "split"
        elif cash is not None:
            kind = "dividend"
        else:
            kind = "split"
        result.setdefault(symbol, {})[event_date] = {
            "kind": kind,
            "ratio": float(row["ratio"]) if row.get("ratio") is not None else None,
            "cash_per_share": cash,
            "event_type": event_type,
        }
    return result


def build_research_series(
    prices: pl.DataFrame,
    events_by_date: dict[date, dict] | None = None,
    *,
    jump_threshold: float = DEFAULT_JUMP_THRESHOLD,
) -> pl.DataFrame:
    """构造单标的研究序列 (输入已按日期升序或由本函数排序).

    Args:
        prices: 列 date/open/high/low/close/volume/amount (原始不复权价)。
        events_by_date: 已核验事件 {date: {kind, ratio, cash_per_share}}。
        jump_threshold: 无事件日原始收益超过该绝对值 → unverified_jump 标记。
    """
    if prices.is_empty():
        return prices
    df = prices.sort("date")
    events_by_date = events_by_date or {}

    dates = df["date"].to_list()
    opens = [float(v) for v in df["open"].to_list()]
    highs = [float(v) for v in df["high"].to_list()]
    lows = [float(v) for v in df["low"].to_list()]
    closes = [float(v) for v in df["close"].to_list()]
    volumes = [float(v) for v in df["volume"].to_list()]
    amounts = [float(v) for v in df["amount"].to_list()]

    rows: list[dict] = []
    split_factor = 1.0
    tr_index = 100.0
    for i, day in enumerate(dates):
        event = events_by_date.get(day)
        ratio = None
        cash = None
        if event:
            if event["kind"] == "split" and event.get("ratio") is not None:
                ratio = float(event["ratio"])
            if event.get("cash_per_share") is not None:
                cash = float(event["cash_per_share"])
        if ratio is not None:
            split_factor *= ratio

        if i == 0:
            research_ret = None
            flag = "first"
        else:
            raw_ret = closes[i] / closes[i - 1] - 1.0
            if ratio is not None and cash is not None:
                research_ret = (closes[i] * ratio + cash) / closes[i - 1] - 1.0
            elif ratio is not None:
                research_ret = (closes[i] * ratio) / closes[i - 1] - 1.0
            elif cash is not None:
                research_ret = (closes[i] + cash) / closes[i - 1] - 1.0
            else:
                research_ret = raw_ret
            if event:
                flag = "verified_event"
            elif abs(raw_ret) > jump_threshold:
                flag = "unverified_jump"
            else:
                flag = "normal"
            tr_index *= 1.0 + research_ret

        rows.append({
            "date": day,
            "open": opens[i], "high": highs[i], "low": lows[i], "close": closes[i],
            "volume": volumes[i], "amount": amounts[i],
            "split_factor": split_factor,
            "adj_open": opens[i] * split_factor,
            "adj_high": highs[i] * split_factor,
            "adj_low": lows[i] * split_factor,
            "adj_close": closes[i] * split_factor,
            "research_ret": research_ret,
            "tr_index": tr_index,
            "quality_flag": flag,
            "event_type": event["event_type"] if event else None,
        })
    return pl.DataFrame(rows, infer_schema_length=None)


def research_price_version(
    events: pl.DataFrame, data_max_date: date | None, code_version: str = "dev",
) -> str:
    """确定性版本: 事件集 + 数据截止 + 代码版本 → rp:v1:<12 hex>."""
    rows = []
    if not events.is_empty():
        for row in events.sort(["symbol", "event_date"]).iter_rows(named=True):
            rows.append({
                "symbol": str(row["symbol"]).zfill(6),
                "event_date": str(row["event_date"]),
                "event_type": row.get("event_type"),
                "ratio": row.get("ratio"),
                "cash_per_share": parse_cash_per_share(row.get("method")),
            })
    payload = json.dumps(
        {"v": 1, "events": rows, "max_date": str(data_max_date), "code": code_version},
        ensure_ascii=False, sort_keys=True, default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"{VERSION_PREFIX}:{digest}"


def _code_version() -> str:
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parents[2], text=True,
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=Path(__file__).resolve().parents[2], text=True,
        ).strip()
        return f"{commit}-dirty" if dirty else commit
    except (OSError, subprocess.CalledProcessError):
        return "dev"


def materialize(
    db_path: str | Path,
    out_dir: str | Path | None = None,
    *,
    symbols: list[str] | None = None,
    jump_threshold: float = DEFAULT_JUMP_THRESHOLD,
    jump_thresholds: dict[str, float] | None = None,
) -> dict:
    """物化研究价格 Parquet 缓存 + `_meta.json`; 返回摘要 (可重复执行)."""
    out_path = Path(out_dir) if out_dir else (
        Path(__file__).resolve().parents[2] / "data" / "cleaned" / "research_prices"
    )
    out_path.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        events = conn.execute(
            "SELECT symbol, event_date, event_type, ratio, method "
            "FROM equity_events WHERE verified = TRUE ORDER BY symbol, event_date"
        ).pl()
        price_df = conn.execute(
            "SELECT symbol, date, open, high, low, close, volume, amount "
            "FROM ohlcv_daily ORDER BY symbol, date"
        ).pl()
    finally:
        conn.close()

    if symbols:
        price_df = price_df.filter(pl.col("symbol").is_in(symbols))
    max_date = price_df["date"].max() if not price_df.is_empty() else None
    code_version = _code_version()
    version = research_price_version(events, max_date, code_version)
    events_by_symbol = prepare_events(events)

    summaries = []
    for symbol in sorted(price_df["symbol"].unique().to_list()):
        threshold = (jump_thresholds or {}).get(symbol, jump_threshold)
        series = build_research_series(
            price_df.filter(pl.col("symbol") == symbol).drop("symbol"),
            events_by_symbol.get(symbol, {}),
            jump_threshold=threshold,
        )
        if series.is_empty():
            continue
        target = out_path / f"{symbol}.parquet"
        series.write_parquet(target)
        flags = series["quality_flag"].value_counts()
        flag_map = dict(zip(flags["quality_flag"], flags["count"], strict=False))
        summaries.append({
            "symbol": symbol,
            "rows": series.height,
            "start": str(series["date"].min()),
            "end": str(series["date"].max()),
            "events": len(events_by_symbol.get(symbol, {})),
            "unverified_jumps": int(flag_map.get("unverified_jump", 0)),
        })

    meta = {
        "version": version,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db": str(db_path),
        "data_max_date": str(max_date) if max_date else None,
        "events_total": events.height,
        "code_version": code_version,
        "jump_threshold": jump_threshold,
        "jump_thresholds": jump_thresholds or {},
        "notes": [
            "adj_* = raw * 累计拆分/合并因子 (量额不调整)",
            "tr_index = 总收益指数 (首日 100; 分红按现金加法; 拆分/合并按比例)",
            "research_ret 为事件调整后的日收益; 组合执行仍用原始价",
            "unverified_jump = 原始收益超阈值且无已核验事件 (不调整, 供下游剔除)",
        ],
        "symbols": summaries,
    }
    (out_path / "_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return meta
