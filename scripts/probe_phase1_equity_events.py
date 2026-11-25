"""Phase 1 P1-M1-01 权益事件核验探针 (只读 + 网络抽样; 不改库、不改价格).

对 `equity_events` 中 12 条 `split_implied` 推断事件, 逐条收集独立证据:
  1) 原始价跳变: 库内 raw prev_close / curr_close (推断值来源, 仅作对照);
  2) 单位净值跳变: `fund_open_fund_info_em` (份额折算时单位净值按同一因子跳变);
  3) 东财后复权隐含因子: `fund_etf_hist_em(adjust='hfq')` 与原始价的比值变化
     (独立数据源的复权处理是否认同该跳变);
  4) 交易所份额变化: `fund_etf_scale_sse` / `fund_scale_daily_szse`
     (折算会使总份额按同一因子变化; 官方交易所统计);
  5) 分红公告检索: `fund_announcement_dividend_em` (排除"其实是分红"的误判)。

纪律: 只采集证据, 不自动把 inferred 升级为 verified; 无法取得的证据记录错误原因。
输出: data/backups/probe_phase1_equity_events_<ts>.json

Usage:
    uv run python scripts/probe_phase1_equity_events.py
    uv run python scripts/probe_phase1_equity_events.py --db <path> --symbols 512100
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import duckdb  # noqa: E402
import pandas as pd  # noqa: E402
from register_equity_events import CURATED_EVENTS  # noqa: E402

logger = logging.getLogger("probe_phase1_equity_events")

OUT_DIR = PROJECT_ROOT / "data" / "backups"
DEFAULT_DB = PROJECT_ROOT / "data" / "quant_pilot.duckdb"


def _sleep() -> None:
    time.sleep(random.uniform(0.3, 0.8))


def _safe(fn, **kwargs) -> tuple[pd.DataFrame | None, str | None]:
    try:
        df = fn(**kwargs)
        _sleep()
        return df, None
    except Exception as exc:  # noqa: BLE001 - 探针必须记录失败而非中断
        _sleep()
        return None, f"{type(exc).__name__}: {str(exc)[:200]}"


def _normalize_symbol(symbol: str) -> str:
    return symbol.strip().split(".")[0].zfill(6)


def _sina_symbol(symbol: str) -> str:
    prefix = "sh" if symbol[0] in "56" else "sz"
    return f"{prefix}{symbol}"


def _to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("-", "")[:8]
    return datetime.strptime(text, "%Y%m%d").date()


def _pick_col(df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    for name in df.columns:
        text = str(name)
        if any(c in text for c in candidates):
            return str(name)
    return None


def read_raw_window(
    conn: duckdb.DuckDBPyConnection, symbol: str, event_date: date, pad_days: int = 15,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT date, open, high, low, close, volume, amount
        FROM ohlcv_daily
        WHERE symbol = ? AND date BETWEEN ? AND ?
        ORDER BY date
        """,
        [symbol, event_date - timedelta(days=pad_days), event_date + timedelta(days=pad_days)],
    ).fetchall()
    return [
        {
            "date": r[0], "open": r[1], "high": r[2], "low": r[3],
            "close": r[4], "volume": r[5], "amount": r[6],
        }
        for r in rows
    ]


def raw_jump_evidence(window: list[dict], event_date: date) -> dict:
    for i, row in enumerate(window):
        if row["date"] == event_date and i > 0:
            prev = window[i - 1]
            return {
                "prev_date": prev["date"].isoformat(),
                "prev_close": prev["close"],
                "event_close": row["close"],
                "raw_ratio": round(prev["close"] / row["close"], 6),
                "raw_return": round(row["close"] / prev["close"] - 1.0, 6),
            }
    return {"error": "event_date 不在库内行情窗口"}


def nav_evidence(symbol: str, event_date: date) -> dict:
    df, err = _safe(ak.fund_open_fund_info_em, symbol=symbol, indicator="单位净值走势")
    if df is None:
        return {"status": "error", "error": err}
    date_col = _pick_col(df, ("净值日期", "日期"))
    nav_col = _pick_col(df, ("单位净值",))
    if date_col is None or nav_col is None:
        return {"status": "error", "error": f"列名不符: {list(df.columns)}"}
    table = df[[date_col, nav_col]].copy()
    table["dt"] = table[date_col].map(_to_date)
    table["nav"] = pd.to_numeric(table[nav_col], errors="coerce")
    table = table.dropna(subset=["nav"]).sort_values("dt")
    before = table[table["dt"] < event_date]
    after = table[table["dt"] >= event_date]
    if before.empty or after.empty:
        return {"status": "error", "error": "事件日前后无净值记录"}
    b, a = before.iloc[-1], after.iloc[0]
    return {
        "status": "ok",
        "nav_before_date": b["dt"].isoformat(),
        "nav_before": float(b["nav"]),
        "nav_after_date": a["dt"].isoformat(),
        "nav_after": float(a["nav"]),
        "nav_ratio": round(float(b["nav"]) / float(a["nav"]), 6),
        "note": "nav_ratio = 事件前净值 / 事件后净值 (price_factor 语义)",
    }


def em_hfq_evidence(symbol: str, event_date: date, source_raw_ratio: float | None) -> dict:
    start = (event_date - timedelta(days=20)).strftime("%Y%m%d")
    end = (event_date + timedelta(days=20)).strftime("%Y%m%d")
    df, err = _safe(
        ak.fund_etf_hist_em,
        symbol=symbol, period="daily", start_date=start, end_date=end, adjust="hfq",
    )
    if df is None:
        return {"status": "error", "error": err}
    date_col = _pick_col(df, ("日期",))
    close_col = _pick_col(df, ("收盘",))
    if date_col is None or close_col is None:
        return {"status": "error", "error": f"列名不符: {list(df.columns)}"}
    table = df[[date_col, close_col]].copy()
    table["dt"] = table[date_col].map(_to_date)
    table["close"] = pd.to_numeric(table[close_col], errors="coerce")
    table = table.dropna(subset=["close"]).sort_values("dt").reset_index(drop=True)
    idx = table.index[table["dt"] == event_date]
    if len(idx) == 0 or idx[0] == 0:
        return {"status": "error", "error": "事件日不在东财 hfq 窗口"}
    i = int(idx[0])
    prev = table.iloc[i - 1]
    curr = table.iloc[i]
    hfq_ratio = float(prev["close"]) / float(curr["close"])
    out = {
        "status": "ok",
        "hfq_prev_date": prev["dt"].isoformat(),
        "hfq_prev_close": float(prev["close"]),
        "hfq_event_close": float(curr["close"]),
        "hfq_ratio": round(hfq_ratio, 6),
    }
    if source_raw_ratio:
        # 若东财按拆分复权, hfq_ratio≈1, implied_factor≈raw_ratio
        out["implied_factor"] = round(source_raw_ratio / hfq_ratio, 6)
    return out


def sse_scale_evidence(symbol: str, event_date: date, raw_window: list[dict]) -> dict:
    trading_days = [row["date"] for row in raw_window]
    prev_days = [d for d in trading_days if d < event_date]
    if not prev_days:
        return {"status": "error", "error": "无前一交易日"}
    prev_day = prev_days[-1]
    snapshots: dict[str, dict] = {}
    for label, day in (("before", prev_day), ("after", event_date)):
        df, err = _safe(ak.fund_etf_scale_sse, date=day.strftime("%Y%m%d"))
        if df is None:
            snapshots[label] = {"error": err}
            continue
        code_col = _pick_col(df, ("基金代码", "代码"))
        share_col = _pick_col(df, ("基金份额", "份额"))
        if code_col is None or share_col is None:
            snapshots[label] = {"error": f"列名不符: {list(df.columns)}"}
            continue
        hit = df[df[code_col].astype(str).str.zfill(6) == symbol]
        if hit.empty:
            snapshots[label] = {"error": "该日快照不含本标的"}
            continue
        snapshots[label] = {
            "date": day.isoformat(),
            "shares": float(pd.to_numeric(hit.iloc[0][share_col], errors="coerce")),
        }
    before = snapshots.get("before", {})
    after = snapshots.get("after", {})
    if "shares" in before and "shares" in after and before["shares"]:
        return {
            "status": "ok",
            "before": before,
            "after": after,
            "scale_factor": round(after["shares"] / before["shares"], 6),
            "note": "scale_factor = 事件后份额 / 事件前份额 (应与 price_factor 一致)",
        }
    return {"status": "error", "snapshots": snapshots}


def szse_scale_evidence(symbol: str, event_date: date) -> dict:
    start = (event_date - timedelta(days=15)).strftime("%Y%m%d")
    end = (event_date + timedelta(days=15)).strftime("%Y%m%d")
    df, err = _safe(ak.fund_scale_daily_szse, start_date=start, end_date=end, symbol="ETF")
    if df is None:
        return {"status": "error", "error": err}
    code_col = _pick_col(df, ("基金代码", "代码"))
    date_col = _pick_col(df, ("日期",))
    share_col = _pick_col(df, ("基金份额", "份额"))
    if code_col is None or date_col is None or share_col is None:
        return {"status": "error", "error": f"列名不符: {list(df.columns)}"}
    table = df[df[code_col].astype(str).str.zfill(6) == symbol].copy()
    if table.empty:
        return {"status": "error", "error": "深市份额表不含本标的"}
    table["dt"] = table[date_col].map(_to_date)
    table["shares"] = pd.to_numeric(table[share_col], errors="coerce")
    table = table.dropna(subset=["shares"]).sort_values("dt")
    before = table[table["dt"] < event_date]
    after = table[table["dt"] >= event_date]
    if before.empty or after.empty:
        return {"status": "error", "error": "事件日前后无份额记录"}
    b, a = before.iloc[-1], after.iloc[0]
    return {
        "status": "ok",
        "before": {"date": b["dt"].isoformat(), "shares": float(b["shares"])},
        "after": {"date": a["dt"].isoformat(), "shares": float(a["shares"])},
        "scale_factor": round(float(a["shares"]) / float(b["shares"]), 6),
        "note": "scale_factor = 事件后份额 / 事件前份额 (应与 price_factor 一致)",
    }


def announcement_evidence(symbol: str, event_date: date) -> dict:
    df, err = _safe(ak.fund_announcement_dividend_em, symbol=symbol)
    if df is None:
        return {"status": "error", "error": err}
    date_col = _pick_col(df, ("公告日期", "日期"))
    title_col = _pick_col(df, ("标题", "公告"))
    if date_col is None or title_col is None:
        return {"status": "ok", "rows": [], "note": f"列名: {list(df.columns)}"}
    table = df.copy()
    table["dt"] = table[date_col].map(_to_date)
    near = table[
        (table["dt"] >= event_date - timedelta(days=10))
        & (table["dt"] <= event_date + timedelta(days=10))
    ]
    rows = [
        {"date": r["dt"].isoformat(), "title": str(r[title_col])[:120]}
        for _, r in near.iterrows()
    ]
    return {"status": "ok", "rows": rows, "total_announcements": int(len(df))}


def _rel_diff(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return abs(a - b) / abs(b)


def _fmt_value(checks: dict, key: str) -> str:
    value = checks.get(key, {}).get("value")
    return "-" if value is None else f"{value:.4f}"


def verdict(evidence: dict, tolerance: float = 0.05) -> dict:
    ratio = evidence["raw"]["raw_ratio"]
    checks = {}
    for key, label in (
        ("nav", "nav_ratio"), ("em_hfq", "implied_factor"),
        ("scale", "scale_factor"),
    ):
        item = evidence.get(key, {})
        value = item.get(label)
        diff = _rel_diff(value, ratio)
        checks[key] = {
            "value": value,
            "rel_diff": None if diff is None else round(diff, 6),
            "within_tolerance": diff is not None and diff <= tolerance,
        }
    corroborated = [k for k, v in checks.items() if v["within_tolerance"]]
    evidence["verdict"] = {
        "inferred_ratio": ratio,
        "tolerance": tolerance,
        "corroborated_by": corroborated,
        "status": "corroborated" if len(corroborated) >= 2 else (
            "partially_corroborated" if corroborated else "unverified"
        ),
        "checks": checks,
    }
    return evidence


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Phase 1 权益事件核验探针")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--symbols", nargs="*", default=None, help="只核验指定代码")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    events = [
        e for e in CURATED_EVENTS
        if args.symbols is None
        or _normalize_symbol(str(e["symbol"])) in { _normalize_symbol(s) for s in args.symbols }
    ]
    if not events:
        raise SystemExit("没有匹配的事件")

    conn = duckdb.connect(args.db, read_only=True)
    results = []
    try:
        for event in events:
            symbol = _normalize_symbol(str(event["symbol"]))
            event_date = _to_date(event["event_date"])
            logger.info("核验 %s %s (inferred ratio=%.3f)", symbol, event_date, event["ratio"])
            window = read_raw_window(conn, symbol, event_date)
            raw = raw_jump_evidence(window, event_date)
            item = {
                "symbol": symbol,
                "event_date": event_date.isoformat(),
                "inferred_ratio": float(event["ratio"]),
                "note": event.get("note"),
                "raw": raw,
                "nav": nav_evidence(symbol, event_date),
                "em_hfq": em_hfq_evidence(
                    symbol, event_date, raw.get("raw_ratio"),
                ),
            }
            if symbol[0] in "56":
                item["scale"] = sse_scale_evidence(symbol, event_date, window)
            else:
                item["scale"] = szse_scale_evidence(symbol, event_date)
            item["announcements"] = announcement_evidence(symbol, event_date)
            if "raw_ratio" in raw:
                verdict(item)
            results.append(item)
    finally:
        conn.close()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.output:
        output = Path(args.output)
    else:
        output = OUT_DIR / f"probe_phase1_equity_events_{stamp}.json"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db": str(args.db),
        "event_count": len(results),
        "tolerance_note": "相对误差 ≤5% 视为与该证据一致; corroborated 需 ≥2 类独立证据",
        "results": results,
    }
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    logger.info("输出: %s", output)

    print(f"\n=== 核验摘要 ({len(results)} 条) ===")
    print(
        f"{'symbol':8s} {'date':12s} {'inferred':>9s} "
        f"{'nav':>8s} {'em_hfq':>8s} {'scale':>8s}  verdict"
    )
    for item in results:
        raw = item["raw"]
        v = item.get("verdict", {})
        checks = v.get("checks", {})
        print(
            f"{item['symbol']:8s} {item['event_date']:12s} "
            f"{raw.get('raw_ratio', float('nan')):9.4f} {_fmt_value(checks, 'nav'):>8s} "
            f"{_fmt_value(checks, 'em_hfq'):>8s} {_fmt_value(checks, 'scale'):>8s}  "
            f"{v.get('status', 'error')}"
        )
    print(f"\n证据 JSON: {output}")


if __name__ == "__main__":
    import akshare as ak  # noqa: E402  (延迟导入, 参数解析失败时不加载)

    main()
