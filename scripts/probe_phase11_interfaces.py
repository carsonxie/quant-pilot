"""Phase 1.1 接口可用性探针 (P11-M1-01) — 真实调用取样, 不写业务库.

对 Phase 1.1 依赖的 akshare 候选接口做一次性真实调用探测:
  core        核心行情: ETF 日线 (Sina) / 股票日线 (Sina+Eastmoney) / 错误代码
  identity    证券身份: 股票全表 / 单只信息 / 基金名称表
  calendar    交易日历
  fundamental 股票财务: 三大报表 (Sina) / 财务指标 / 金融企业样本
  valuation   股票估值: PE/PB 历史 (候选接口)
  etf         ETF 扩展: 净值历史 / 分红 / 现货快照

纪律 (spec §8.4.3, plan-phase1.1 P11-M1):
  - 结论以真实调用为准; 失败/空响应/超时如实记录, 不做压力抓取, 不重试凑成功率。
  - 每接口 1 次调用 (错误样本单列); 每次调用后休眠, Eastmoney 接口间隔更大。
  - 只读探测: 不写 DuckDB, 唯一输出为 data/backups/probe_phase11_<ts>.json。

Usage:
    uv run python scripts/probe_phase11_interfaces.py                  # 全部组
    uv run python scripts/probe_phase11_interfaces.py --group core identity
    uv run python scripts/probe_phase11_interfaces.py --timeout 60
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import akshare as ak
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import data_pipeline.fetchers.akshare_fetcher  # noqa: F401, E402  (代理绕过补丁, 与生产路径一致)

OUT_DIR = PROJECT_ROOT / "data" / "backups"

# 日期类列的候选名称 (用于抽取日期范围)
_DATE_COL_HINTS = ("date", "日期", "净值日期", "报告期", "报告日", "trade_date", "时间")

Probe = tuple[str, str, str, Callable[[], object], float]


# ------------------------------------------------------------------
# 结果压缩
# ------------------------------------------------------------------

def _jsonable(obj: object, depth: int = 0) -> object:
    """递归转 JSON 可序列化 (截断长字符串, NaN → None)."""
    if depth > 6:
        return "..."
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return None if math.isnan(obj) else obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, str):
        return obj[:200]
    if isinstance(obj, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in list(obj.items())[:40]}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v, depth + 1) for v in list(obj)[:5]]
    return str(obj)[:200]


def _describe(value: object) -> dict:
    """把返回值压缩为可 JSON 化的结构 (行数/列/日期范围/首尾样本/空值)."""
    if isinstance(value, pd.DataFrame):
        info: dict = {
            "kind": "dataframe",
            "rows": int(len(value)),
            "columns": [str(c) for c in value.columns],
        }
        if len(value) > 0:
            info["head"] = _jsonable(value.head(2).to_dict(orient="records"))
            info["tail"] = _jsonable(value.tail(3).to_dict(orient="records"))
            for col in value.columns:
                if str(col) in _DATE_COL_HINTS:
                    parsed = pd.to_datetime(value[col].astype(str), errors="coerce")
                    if parsed.notna().any():
                        info["date_col"] = str(col)
                        info["date_min"] = str(parsed.min())[:10]
                        info["date_max"] = str(parsed.max())[:10]
                    break
            nulls = {
                str(k): int(v) for k, v in value.isna().sum().items() if int(v) > 0
            }
            if nulls:
                info["null_counts"] = nulls
        return info
    if isinstance(value, dict):
        return {
            "kind": "dict",
            "keys": [str(k) for k in value],
            "sample": _jsonable({str(k): value[k] for k in list(value)[:15]}),
        }
    return {"kind": type(value).__name__, "repr": repr(value)[:300]}


# ------------------------------------------------------------------
# 探测执行 (线程超时; 挂起调用不阻塞后续探测)
# ------------------------------------------------------------------

def _probe(name: str, group: str, call_desc: str, fn: Callable[[], object], timeout: int) -> dict:
    record: dict = {"group": group, "name": name, "call": call_desc}
    start = time.time()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(fn)
        value = future.result(timeout=timeout)
        is_empty = value is None or (isinstance(value, pd.DataFrame) and value.empty)
        record["status"] = "empty" if is_empty else "ok"
        record.update(_describe(value))
    except concurrent.futures.TimeoutError:
        record["status"] = "timeout"
        record["error"] = f"no return within {timeout}s"
    except Exception as exc:
        record["status"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"[:400]
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        record["latency_s"] = round(time.time() - start, 2)
    return record


# ------------------------------------------------------------------
# 探测清单 (候选接口, 以实测为准)
# ------------------------------------------------------------------

def _nav_history(fund: str) -> pd.DataFrame:
    """净值历史: 兼容不同版本的参数签名 (截至调用日)."""
    end = time.strftime("%Y%m%d")
    try:
        return ak.fund_etf_fund_info_em(
            fund=fund, start_date="20240101", end_date=end,
        )
    except TypeError:
        return ak.fund_etf_fund_info_em(fund=fund)


def _fin_indicator() -> pd.DataFrame:
    """财务指标: 兼容不同版本的参数签名."""
    try:
        return ak.stock_financial_analysis_indicator(symbol="600519", start_year="2023")
    except TypeError:
        return ak.stock_financial_analysis_indicator("600519")


def _probes() -> list[Probe]:
    return [
        # ---- core: 核心行情 ----
        ("core", "etf_daily_sina_510300", "ak.fund_etf_hist_sina(symbol='sh510300')",
         lambda: ak.fund_etf_hist_sina(symbol="sh510300"), 1.2),
        ("core", "etf_daily_sina_513100_cross", "ak.fund_etf_hist_sina(symbol='sh513100')",
         lambda: ak.fund_etf_hist_sina(symbol="sh513100"), 1.2),
        ("core", "stock_daily_sina_600519_raw", "ak.stock_zh_a_daily(symbol='sh600519', adjust='')",
         lambda: ak.stock_zh_a_daily(symbol="sh600519", adjust=""), 1.2),
        ("core", "stock_daily_sina_600519_qfq",
         "ak.stock_zh_a_daily(symbol='sh600519', adjust='qfq')",
         lambda: ak.stock_zh_a_daily(symbol="sh600519", adjust="qfq"), 1.2),
        ("core", "stock_daily_short_688981", "ak.stock_zh_a_daily(symbol='sh688981', adjust='')",
         lambda: ak.stock_zh_a_daily(symbol="sh688981", adjust=""), 1.2),
        ("core", "stock_daily_em_600036",
         "ak.stock_zh_a_hist(symbol='600036', period='daily', start_date='20260101', "
         "end_date='20260926', adjust='')",
         lambda: ak.stock_zh_a_hist(
             symbol="600036", period="daily", start_date="20260101",
             end_date="20260926", adjust="",
         ), 2.5),
        ("core", "bad_code_stock_sz999999", "ak.stock_zh_a_daily(symbol='sz999999', adjust='')",
         lambda: ak.stock_zh_a_daily(symbol="sz999999", adjust=""), 1.2),
        ("core", "bad_code_etf_sh999999", "ak.fund_etf_hist_sina(symbol='sh999999')",
         lambda: ak.fund_etf_hist_sina(symbol="sh999999"), 1.2),

        # ---- identity: 证券身份 ----
        ("identity", "stock_list_code_name", "ak.stock_info_a_code_name()",
         lambda: ak.stock_info_a_code_name(), 2.5),
        ("identity", "stock_individual_info_em_600519",
         "ak.stock_individual_info_em(symbol='600519')",
         lambda: ak.stock_individual_info_em(symbol="600519"), 2.5),
        ("identity", "fund_name_em", "ak.fund_name_em()",
         lambda: ak.fund_name_em(), 2.5),

        # ---- calendar: 交易日历 ----
        ("calendar", "trade_date_hist_sina", "ak.tool_trade_date_hist_sina()",
         lambda: ak.tool_trade_date_hist_sina(), 1.2),

        # ---- fundamental: 股票财务 ----
        ("fundamental", "fin_income_600519",
         "ak.stock_financial_report_sina(stock='sh600519', symbol='利润表')",
         lambda: ak.stock_financial_report_sina(stock="sh600519", symbol="利润表"), 1.5),
        ("fundamental", "fin_balance_600519",
         "ak.stock_financial_report_sina(stock='sh600519', symbol='资产负债表')",
         lambda: ak.stock_financial_report_sina(stock="sh600519", symbol="资产负债表"), 1.5),
        ("fundamental", "fin_cashflow_600519",
         "ak.stock_financial_report_sina(stock='sh600519', symbol='现金流量表')",
         lambda: ak.stock_financial_report_sina(stock="sh600519", symbol="现金流量表"), 1.5),
        ("fundamental", "fin_indicator_600519",
         "ak.stock_financial_analysis_indicator(symbol='600519', start_year='2023')",
         _fin_indicator, 2.0),
        ("fundamental", "fin_income_601398_bank",
         "ak.stock_financial_report_sina(stock='sh601398', symbol='利润表')",
         lambda: ak.stock_financial_report_sina(stock="sh601398", symbol="利润表"), 1.5),

        # ---- valuation: 股票估值 ----
        ("valuation", "stock_a_indicator_lg_600519", "ak.stock_a_indicator_lg(symbol='600519')",
         lambda: ak.stock_a_indicator_lg(symbol="600519"), 2.0),

        # ---- etf: ETF 扩展 ----
        ("etf", "etf_nav_510300",
         "ak.fund_etf_fund_info_em(fund='510300', start_date='20240101', end_date='20260926')",
         lambda: _nav_history("510300"), 2.0),
        ("etf", "etf_nav_513100_cross",
         "ak.fund_etf_fund_info_em(fund='513100', ...)",
         lambda: _nav_history("513100"), 2.0),
        ("etf", "etf_dividend_sina_510300", "ak.fund_etf_dividend_sina(symbol='sh510300')",
         lambda: ak.fund_etf_dividend_sina(symbol="sh510300"), 1.5),
        ("etf", "etf_spot_em", "ak.fund_etf_spot_em()",
         lambda: ak.fund_etf_spot_em(), 3.0),

        # ---- extra: 首轮失败/缺失项的替选 (2026-09-28 补测) ----
        ("extra", "fund_etf_fund_daily_em", "ak.fund_etf_fund_daily_em()",
         lambda: ak.fund_etf_fund_daily_em(), 3.0),
        ("extra", "fund_etf_category_sina", "ak.fund_etf_category_sina(symbol='ETF基金')",
         lambda: ak.fund_etf_category_sina(symbol="ETF基金"), 1.5),
        ("extra", "fund_open_fund_info_em_510300_nav",
         "ak.fund_open_fund_info_em(symbol='510300', indicator='单位净值走势')",
         lambda: ak.fund_open_fund_info_em(
             symbol="510300", indicator="单位净值走势",
         ), 2.0),
        ("extra", "fund_etf_scale_sse", "ak.fund_etf_scale_sse(date='20250115')",
         lambda: ak.fund_etf_scale_sse(date="20250115"), 2.0),
        ("extra", "fund_etf_scale_szse", "ak.fund_etf_scale_szse()",
         lambda: ak.fund_etf_scale_szse(), 2.0),
        ("extra", "stock_value_em_600519", "ak.stock_value_em(symbol='600519')",
         lambda: ak.stock_value_em(symbol="600519"), 2.5),
        ("extra", "stock_zh_valuation_baidu_600519_pe",
         "ak.stock_zh_valuation_baidu(symbol='600519', indicator='市盈率(TTM)', period='近一年')",
         lambda: ak.stock_zh_valuation_baidu(
             symbol="600519", indicator="市盈率(TTM)", period="近一年",
         ), 2.0),
        ("extra", "stock_zh_valuation_baidu_600519_pb",
         "ak.stock_zh_valuation_baidu(symbol='600519', indicator='市净率', period='近一年')",
         lambda: ak.stock_zh_valuation_baidu(
             symbol="600519", indicator="市净率", period="近一年",
         ), 2.0),
        ("extra", "fund_etf_hist_em_510300",
         "ak.fund_etf_hist_em(symbol='510300', period='daily', start_date='20260101', "
         "end_date='20260928', adjust='qfq')",
         lambda: ak.fund_etf_hist_em(
             symbol="510300", period="daily", start_date="20260101",
             end_date="20260928", adjust="qfq",
         ), 2.5),
        ("extra", "fund_etf_spot_ths", "ak.fund_etf_spot_ths()",
         lambda: ak.fund_etf_spot_ths(), 3.0),

        # ---- extra2: ETF 资料/费率 与规模近期日期 (2026-09-28 追加) ----
        ("extra2", "fund_overview_em_510300", "ak.fund_overview_em(symbol='510300')",
         lambda: ak.fund_overview_em(symbol="510300"), 2.5),
        ("extra2", "fund_overview_em_513100", "ak.fund_overview_em(symbol='513100')",
         lambda: ak.fund_overview_em(symbol="513100"), 2.5),
        ("extra2", "fund_etf_scale_sse_recent",
         "ak.fund_etf_scale_sse(date='20260925')",
         lambda: ak.fund_etf_scale_sse(date="20260925"), 2.0),
        ("extra2", "fund_individual_basic_info_xq_510300",
         "ak.fund_individual_basic_info_xq(symbol='510300')",
         lambda: ak.fund_individual_basic_info_xq(symbol="510300"), 2.0),
    ]


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

_FLAG = {"ok": "OK", "empty": "EMPTY", "error": "ERR", "timeout": "TIMEOUT"}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Phase 1.1 接口可用性探针 (只读)")
    parser.add_argument("--group", nargs="*", default=None,
                        help="只运行指定组: core identity calendar fundamental valuation "
                             "etf extra extra2")
    parser.add_argument("--timeout", type=int, default=60, help="单接口超时秒数 (默认 60)")
    parser.add_argument("--out", default=None, help="输出 JSON 路径 (默认 data/backups/)")
    args = parser.parse_args()

    probes = _probes()
    if args.group:
        wanted = set(args.group)
        probes = [p for p in probes if p[0] in wanted]
    if not probes:
        print("没有匹配的探测项", file=sys.stderr)
        sys.exit(2)

    print(f"Phase 1.1 接口探针: {len(probes)} 项, 单接口超时 {args.timeout}s\n")
    results: list[dict] = []
    for group, name, desc, fn, pause in probes:
        record = _probe(name, group, desc, fn, args.timeout)
        results.append(record)
        flag = _FLAG[record["status"]]
        if record["status"] in ("ok", "empty"):
            extra = f"rows={record.get('rows', '-')} cols={len(record.get('columns', []))}"
        else:
            extra = record.get("error", "")[:140]
        print(f"[{flag:7s}] {group:11s} {name:30s} {record['latency_s']:6.1f}s  {extra}",
              flush=True)
        time.sleep(pause)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "akshare": getattr(ak, "__version__", "?"),
        "timeout_s": args.timeout,
        "probes": results,
    }
    out = Path(args.out) if args.out else (
        OUT_DIR / f"probe_phase11_{datetime.now():%Y%m%d_%H%M%S}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    n_ok = sum(1 for r in results if r["status"] == "ok")
    n_empty = sum(1 for r in results if r["status"] == "empty")
    n_err = sum(1 for r in results if r["status"] in ("error", "timeout"))
    print(f"\n汇总: {len(results)} 项 → ok={n_ok} empty={n_empty} error/timeout={n_err}")
    print(f"输出: {out}")
    sys.stdout.flush()
    # 超时后仍挂起的探测线程不阻塞进程退出 (一次性探测脚本)
    os._exit(0)


if __name__ == "__main__":
    main()
