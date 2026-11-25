"""物化版本化研究价格序列 (P1-M1-02).

读取 settings 的涨跌幅板配置, 生成产品感知阈值 (10% 板+2pp / 20% 板+2pp),
调用 `data_pipeline.research_prices.materialize` 写 Parquet 缓存 + `_meta.json`,
并逐条打印已核验事件日的事件调整收益 (供连续性核对)。

Usage:
    uv run python scripts/materialize_research_prices.py
    uv run python scripts/materialize_research_prices.py --symbols 512100 512690
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import duckdb  # noqa: E402
import polars as pl  # noqa: E402
import yaml  # noqa: E402

from data_pipeline.research_prices import materialize  # noqa: E402


def _thresholds(settings: dict) -> tuple[float, dict[str, float]]:
    cfg = settings.get("data_quality", {}).get("daily_change", {})
    default = float(cfg.get("default_limit_pct", 10.0)) / 100.0
    buffer = float(cfg.get("review_buffer_pct", 2.0)) / 100.0
    overrides = {
        str(symbol).zfill(6): float(pct) / 100.0 + buffer
        for symbol, pct in cfg.get("limit_overrides_pct", {}).items()
    }
    return default + buffer, overrides


def _event_check(db_path: str, out_dir: Path) -> None:
    conn = duckdb.connect(db_path, read_only=True)
    try:
        events = conn.execute(
            "SELECT symbol, event_date, event_type, ratio, verified "
            "FROM equity_events ORDER BY symbol, event_date"
        ).fetchall()
    finally:
        conn.close()
    print("\n=== 事件日收益连续性核对 (原始 vs 事件调整) ===")
    for symbol, event_date, event_type, ratio, verified in events:
        path = out_dir / f"{symbol}.parquet"
        if not path.exists():
            print(f"  {symbol} {event_date}: 缺少 Parquet")
            continue
        series = pl.read_parquet(path).sort("date")
        hit = series.filter(pl.col("date") == event_date)
        if hit.is_empty():
            print(f"  {symbol} {event_date}: 序列中无该日")
            continue
        idx = series.with_row_index("_i").filter(pl.col("date") == event_date)["_i"][0]
        if idx == 0:
            print(f"  {symbol} {event_date}: 首日, 无前收")
            continue
        row = series.row(int(idx), named=True)
        prev_close = float(series.row(int(idx) - 1, named=True)["close"])
        raw_ret = float(row["close"]) / prev_close - 1.0
        print(
            f"  {symbol} {event_date} {event_type:17s} ratio={ratio:<10.6g} "
            f"verified={verified} raw={raw_ret:+.2%} "
            f"research={row['research_ret']:+.2%} tr={row['tr_index']:.2f}"
        )


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="物化研究价格序列")
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--symbols", nargs="*", default=None)
    args = parser.parse_args()

    with open(PROJECT_ROOT / "config" / "settings.yaml", encoding="utf-8") as fh:
        settings = yaml.safe_load(fh)
    db_path = args.db or str(PROJECT_ROOT / settings["storage"]["db_path"])
    out_dir = Path(args.out) if args.out else PROJECT_ROOT / "data" / "cleaned" / "research_prices"
    default_threshold, overrides = _thresholds(settings)

    meta = materialize(
        db_path, out_dir,
        symbols=args.symbols,
        jump_threshold=default_threshold,
        jump_thresholds=overrides,
    )
    print(f"版本: {meta['version']}")
    print(
        f"数据截止: {meta['data_max_date']} | 事件数: {meta['events_total']} "
        f"| 标的数: {len(meta['symbols'])}"
    )
    print(f"阈值: 默认 {default_threshold:.0%}; 20% 板 {overrides}")
    flagged = [row for row in meta["symbols"] if row["unverified_jumps"]]
    print(f"未核验跳变标记: {len(flagged)} 个标的")
    for row in flagged:
        print(f"  {row['symbol']}: {row['unverified_jumps']} 处")
    _event_check(db_path, out_dir)
    print(f"\n输出目录: {out_dir}")


if __name__ == "__main__":
    main()
