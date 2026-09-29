"""权益事件登记 (M8/I-09 方案1) — 把未调整权益事件跳变登记为"预期跳变日历".

背景: Sina 返回不复权价格, 拆分/份额折算会造成单日巨幅跳变 (如 ÷5 折算)。
每日增量质检遇此类跳变按 critical 阻断 (隔离设计, C10); 全量 fetch-data 质检
每次都会报出历史跳变。本脚本把**已人工核验**的跳变登记到 `equity_events`
表 (schema v7), 使质检按"已登记权益事件"放行 (info, 不阻断), 未登记跳变
仍照常 critical 喊人。

语义与约束:
  - ratio 倍率, `price_factor`: 期望"后一收盘 ≈ 前一收盘 / ratio"(1:2 拆分 → 2.0);
    反向折算 (份额合并) 时 ratio < 1。
  - **只登记, 绝不修改价格数据**; 登记表仅作"预期跳变日历"。
  - 登记比例由跳变反推 (`inferred=True, verified=False`), 待官方数据/人工核验后更新;
    官方数据接入 (Phase 1, I-09 方案2) 后可将来源升级为 `official`。
  - 登记后自动复核: 用登记表重跑质检, 若同一跳变仍为 critical 则回滚 (不落库)。
  - 不在本脚本登记的项目 (幅度小/疑似数据源误差): 维持人工核验。

Usage:
    uv run python scripts/register_equity_events.py --check          # 只列出未登记的巨幅跳变
    uv run python scripts/register_equity_events.py --apply          # 登记已核验清单 (幂等)
    uv run python scripts/register_equity_events.py --apply --auto   # 额外自动登记 all 巨幅跳变
    uv run python scripts/register_equity_events.py --db <path> ...

注意: 自动模式会把所有超限跳变登记为"推断值", 仅在人工逐条核验跳变性质后使用。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import polars as pl  # noqa: E402

from data_pipeline.duckdb_manager import DuckDBManager  # noqa: E402
from data_pipeline.processors.validator import validate_ohlcv  # noqa: E402

logger = logging.getLogger("register_equity_events")

# 已核验清单 (2026-09-18 从库内跳变核对; 比例=raw prev_close/curr_close, 推断值待官方核验)
# 排除项 (未登记, 维持人工核验): 512690 2021-12-31 (−27%), 510500 2022-08-29 (−13%)
CURATED_EVENTS: list[dict] = [
    {"symbol": "159928", "event_date": date(2021, 6, 25), "ratio": 3.918,
     "note": "消费ETF 份额折算 (推断值待核验)"},
    {"symbol": "512010", "event_date": date(2021, 6, 28), "ratio": 3.835,
     "note": "医药ETF 份额折算 (推断值待核验)"},
    {"symbol": "512690", "event_date": date(2021, 5, 17), "ratio": 1.923,
     "note": "酒ETF 份额折算 (推断值待核验)"},
    {"symbol": "512690", "event_date": date(2021, 12, 31), "ratio": 1.371,
     "note": (
         "酒ETF 份额折算: 前收 1.323 → 当日开盘 0.976 (缺口全部在开盘, "
         "成交量正常), 符合折算特征; 推断值待核验"
     )},
    {"symbol": "512480", "event_date": date(2021, 3, 29), "ratio": 1.957,
     "note": "半导体ETF 份额折算 (推断值待核验)"},
    {"symbol": "513100", "event_date": date(2022, 1, 14), "ratio": 5.115,
     "note": "纳指ETF 份额折算 (推断值待核验)"},
    {"symbol": "513500", "event_date": date(2022, 3, 30), "ratio": 1.968,
     "note": "标普500ETF 份额折算 (推断值待核验)"},
    {"symbol": "159941", "event_date": date(2022, 7, 5), "ratio": 3.947,
     "note": "纳指ETF(广发) 份额折算 (推断值待核验)"},
    {"symbol": "516160", "event_date": date(2024, 9, 18), "ratio": 0.311,
     "note": "新能源ETF 反向折算(份额合并) (推断值待核验)"},
    {"symbol": "512100", "event_date": date(2022, 9, 5), "ratio": 0.362,
     "note": "中证1000ETF 反向折算; ⚠️ 因子偏大, 需优先核验"},
    {"symbol": "512480", "event_date": date(2026, 7, 3), "ratio": 2.029,
     "note": "半导体ETF 第二次份额折算 (推断值待核验)"},
    {"symbol": "159995", "event_date": date(2026, 7, 7), "ratio": 2.005,
     "note": "芯片ETF 份额折算 (推断值待核验)"},
]


def detect_jumps(
    db: DuckDBManager,
    price_limits: dict[str, float] | None = None,
    default_limit: float = 0.10,
    buffer: float = 0.02,
    min_abs: float = 0.15,
) -> pl.DataFrame:
    """扫描库内超限巨幅跳变 (超出该产品涨跌板, 产品感知).

    20% 板产品 (159915/588000) 的 ±20% 真实移动不视为跳变 (limit 覆盖)。
    """
    limits = price_limits or {}
    df = db.get_ohlcv()
    if df.is_empty():
        return pl.DataFrame()
    rows: list[dict] = []
    for sym in df["symbol"].unique().to_list():
        limit = float(limits.get(sym, default_limit))
        threshold = max(limit + buffer, min_abs)
        s = df.filter(pl.col("symbol") == sym).sort("date")
        closes = s["close"].to_list()
        dates = s["date"].to_list()
        for i in range(1, len(closes)):
            prev, curr = closes[i - 1], closes[i]
            if prev is None or curr is None or prev <= 0 or curr <= 0:
                continue
            ret = curr / prev - 1.0
            if abs(ret) > threshold:
                rows.append({
                    "symbol": sym,
                    "event_date": dates[i],
                    "ret": ret,
                    "ratio_raw": prev / curr,
                })
    return pl.DataFrame(rows)


def register(
    db: DuckDBManager,
    events: list[dict],
    check_after_apply: bool = True,
) -> int:
    """登记事件并复核 (不可用则回滚). Returns: 登记行数."""
    payload = pl.DataFrame([
        {
            "symbol": e["symbol"], "event_date": e["event_date"],
            "event_type": "split_implied", "ratio": float(e["ratio"]),
            "ratio_semantics": "price_factor",
            "source": "inferred_from_price", "inferred": True, "verified": False,
            "method": "全量跳变核对 (raw prev_close/curr_close)",
            "note": e.get("note"),
        }
        for e in events
    ])
    with db.transaction():
        n = db.upsert_equity_events(payload)
        if check_after_apply:
            # 复核: 登记后同一跳变不应再是 critical (未命中 → 回滚)
            issues = validate_ohlcv(
                db.get_ohlcv(), registered_equity_events=db.get_equity_events(),
            )
            criticals = [i for i in issues if i.severity == "critical"]
            if criticals:
                # 仅当 critical 落在本次登记标的上才回滚 (其余问题与本次无关)
                targets = {(e["symbol"], e["event_date"]) for e in events}
                residual = [
                    i for i in criticals
                    if i.symbol in {t[0] for t in targets}
                ]
                if residual:
                    details = "; ".join(
                        f"{i.symbol}: {i.detail[:80]}" for i in residual[:3]
                    )
                    raise ValueError(
                        f"登记复核失败 (同一标的存在残余 critical), 已回滚: {details}"
                    )
        return n


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="权益事件登记 (M8/I-09 方案1): 拆分感知质检的'预期跳变日历'",
    )
    parser.add_argument("--db", default=None, help="DuckDB 路径 (缺省读 settings)")
    parser.add_argument("--check", action="store_true", help="只列出未登记的巨幅跳变")
    parser.add_argument("--apply", action="store_true", help="登记已核验清单 (幂等)")
    parser.add_argument(
        "--auto", action="store_true",
        help="附带自动登记: 把全部未登记巨幅跳变按反推比例登记 (需先人工核验)",
    )
    args = parser.parse_args()

    import yaml

    with open(PROJECT_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    db_path = args.db or str(PROJECT_ROOT / settings["storage"]["db_path"])

    db = DuckDBManager(db_path)
    db.init_database()
    try:
        registered = db.get_equity_events()
        reg_keys = {
            (str(r["symbol"]), r["event_date"])
            for r in registered.iter_rows(named=True)
        } if not registered.is_empty() else set()

        # 产品感知阈值 (20% 板覆盖): 159915/588000 的真实 ±20% 不算跳变
        dq_cfg = settings.get("data_quality", {}).get("daily_change", {})
        price_limits = {
            sym: float(pct) / 100.0
            for sym, pct in dq_cfg.get("limit_overrides_pct", {}).items()
        }
        jumps = detect_jumps(db, price_limits=price_limits)
        if jumps.is_empty():
            print("库内无巨幅跳变。")
            return
        unregistered = jumps.filter(
            ~pl.struct(["symbol", "event_date"]).map_elements(
                lambda s: (str(s["symbol"]), s["event_date"]) in reg_keys,
                return_dtype=pl.Boolean,
            )
        )

        print(f"=== 已登记 {len(reg_keys)} 条 | 库内巨幅跳变 {len(jumps)} 条 "
              f"| 未登记 {len(unregistered)} 条 ===")
        for r in unregistered.sort(["symbol", "event_date"]).iter_rows(named=True):
            print(
                f"  {r['symbol']} {r['event_date']} ret={r['ret']:+.2%} "
                f"反推 ratio≈{r['ratio_raw']:.3f}"
            )

        if args.apply:
            events = list(CURATED_EVENTS)
            if args.auto:
                curated_keys = {(e["symbol"], e["event_date"]) for e in events}
                for r in unregistered.iter_rows(named=True):
                    key = (str(r["symbol"]), r["event_date"])
                    if key in curated_keys:
                        continue
                    ratio = float(r["ratio_raw"])
                    note = "自动登记 (推断值待核验; --auto)"
                    if 0.9 <= ratio <= 1.1:
                        note = "自动登记: ⚠️ 反推因子≈1 (幅度小), 建议人工核验后更新或删除"
                    events.append({
                        "symbol": str(r["symbol"]),
                        "event_date": r["event_date"],
                        "ratio": round(ratio, 3),
                        "note": note,
                    })
            n = register(db, events)
            print(f"登记完成: {n} 条 (幂等 UPSERT); 复核通过 (无残余 critical)")

        if not args.apply and not args.check:
            print("\n提示: 使用 --apply 登记已核验清单; --apply --auto 额外自动登记全部跳变。")
    finally:
        db.close()


if __name__ == "__main__":
    main()
