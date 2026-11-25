"""Phase 1 P1-M1-01 权益事件核验 (官方公告正文 → 结构化证据).

对 12 条 `equity_events` 逐条检索基金公告 (东财基金公告 feed, type=1 全部),
抓取"拆分/合并结果"或"分红"公告正文, 提取:
  - 官方拆分/合并比例 (price_factor 语义: 价格 = 前收 / ratio);
  - 分红事件的每股现金 (元/份);
  - 公告 ID / 日期 / 标题 (可追溯来源)。

不写库: 只产出 `data/backups/equity_event_verification_<ts>.json` 与摘要表;
库内 `verified` 升级与 research price 由后续受控步骤执行。

Usage:
    uv run python scripts/verify_equity_events.py
    uv run python scripts/verify_equity_events.py --symbols 512100 512690
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import requests  # noqa: E402
from register_equity_events import CURATED_EVENTS  # noqa: E402

ANN_URL = "http://api.fund.eastmoney.com/f10/JJGG"
CONTENT_URL = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "http://fundf10.eastmoney.com/",
}
OUT_DIR = PROJECT_ROOT / "data" / "backups"
CURATED_PATH = OUT_DIR / "equity_event_verification_curated.json"

_CASH_PATTERNS = [
    re.compile(r"每\s*10\s*份[^0-9]{0,30}([0-9.]+)\s*元"),
    re.compile(r"每份[^0-9]{0,20}([0-9.]+)\s*元"),
    re.compile(r"元\s*/\s*10\s*份[^0-9]{0,20}([0-9.]+)"),
]
_BEFORE_SHARES = re.compile(
    r"(?:拆\s*分|合\s*并)前[^0-9]{0,80}(?:份额总额|份额总数)为\s*([0-9,]+(?:\.\d+)?)\s*份"
)
_AFTER_SHARES = re.compile(
    r"(?:拆\s*分|合\s*并)后[^0-9]{0,80}(?:份额总额|份额总数)为\s*([0-9,]+(?:\.\d+)?)\s*份"
)

# 基金公司公告不在东财权益公告 feed 内的已核验条目 (来源: 基金公司公告 PDF)
OFFICIAL_OVERRIDES: dict[tuple[str, date], dict] = {
    ("512100", date(2022, 9, 5)): {
        "official_ratio": 0.36555,
        "cash_per_share": None,
        "source": "fund_company_pdf",
        "announcement": {
            "date": "2022-09-02",
            "title": (
                "关于南方中证1000交易型开放式指数证券投资基金基金份额合并结果"
                "及恢复交易和申购、赎回的公告"
            ),
            "id": "nffund://DownLoader.java?filePath=/main/files/2022/09/02/252556513557.pdf",
        },
        "note": (
            "2022-09-02 份额合并 (权益登记日 09-01); 公告 PDF 载明合并比例 0.36555; "
            "2022-07-30 已公告取消原拆分计划; 交易所份额 10,207,092,000→3,628,217,600"
        ),
    },
    ("510500", date(2022, 8, 29)): {
        "official_ratio": 1.14542,
        "cash_per_share": None,
        "source": "official_announcement",
        "announcement": {
            "date": "2022-08-29",
            "title": "关于中证500交易型开放式指数证券投资基金基金份额拆分结果的公告",
            "id": "AN202208291577764682",
        },
        "note": (
            "拆分日 2022-08-26; 拆分前 5,173,467,244 份 → 拆分后 5,925,768,617 份; "
            "原未登记边界项 (-12.74%), 本次核验后应登记"
        ),
    },
}

# 未登记但需核验的边界项 (不写入 CURATED_EVENTS, 仅作证据)
EXTRA_EVENTS: list[dict] = [
    {"symbol": "510500", "event_date": date(2022, 8, 29), "ratio": 1.14603,
     "note": "边界项: 2022-08-29 -12.74% (未登记); 2022-08-26 份额拆分"},
]


def _session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False  # EV-004: 绕过不可达系统代理
    return session


def fetch_announcements(session: requests.Session, symbol: str) -> list[dict]:
    response = session.get(
        ANN_URL,
        params={
            "fundcode": symbol, "pageIndex": "1", "pageSize": "1000",
            "type": "2", "_": int(time.time() * 1000),
        },
        headers=HEADERS, timeout=20,
    )
    payload = response.json()
    rows = payload.get("Data") or []
    time.sleep(0.3)
    return [
        {
            "date": datetime.strptime(str(row["PUBLISHDATEDesc"])[:10], "%Y-%m-%d").date(),
            "title": str(row.get("TITLE", "")),
            "id": str(row.get("ID", "")),
        }
        for row in rows
    ]


def fetch_content(session: requests.Session, art_code: str) -> str:
    response = session.get(
        CONTENT_URL,
        params={"art_code": art_code, "client_source": "web", "page_index": "1"},
        headers=HEADERS, timeout=20,
    )
    time.sleep(0.3)
    return str((response.json().get("data") or {}).get("notice_content") or "")


def _to_float(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def extract_official(text: str) -> dict:
    ratio = None
    # 1:N 拆分 → 份额 ×N, 价格 ÷N → price_factor = N
    match = re.search(r"拆\s*分\s*比\s*例\s*为\s*1\s*[:：]\s*([0-9.]+)", text)
    if match:
        ratio = _to_float(match.group(1))
    # 合并比例 X (份额 ×X, 价格 ÷X) → price_factor = X
    if ratio is None:
        match = re.search(r"合\s*并\s*比\s*例\s*为\s*([0-9.]+)", text)
        if match:
            ratio = _to_float(match.group(1))
    # N:1 合并的另一种写法 → price_factor = 1/N
    if ratio is None:
        match = re.search(r"(?:拆\s*分|合\s*并)\s*比\s*例\s*为\s*([0-9.]+)\s*[:：]\s*1", text)
        if match:
            value = _to_float(match.group(1))
            ratio = 1.0 / value if value else None
    if ratio is None:
        before = _BEFORE_SHARES.search(text)
        after = _AFTER_SHARES.search(text)
        b = _to_float(before.group(1)) if before else None
        a = _to_float(after.group(1)) if after else None
        if b and a:
            ratio = a / b
    cash = None
    for pattern in _CASH_PATTERNS:
        match = pattern.search(text)
        if match:
            value = _to_float(match.group(1))
            if value:
                cash = value / 10.0 if "10" in pattern.pattern else value
                break
    return {"official_ratio": ratio, "cash_per_share": cash}


def pick_candidate(announcements: list[dict], event_date: date) -> dict | None:
    window_start = event_date - timedelta(days=40)
    keywords = ("拆分结果", "合并结果", "分红", "份额折算结果")
    candidates = [
        row for row in announcements
        if window_start <= row["date"] <= event_date + timedelta(days=1)
        and any(key in row["title"] for key in keywords)
    ]
    if not candidates:
        candidates = [
            row for row in announcements
            if window_start <= row["date"] <= event_date + timedelta(days=1)
            and any(key in row["title"] for key in ("拆分", "合并", "折算"))
        ]
    return sorted(candidates, key=lambda row: row["date"])[-1] if candidates else None


def _status_rank(status: str | None) -> int:
    return 2 if status in ("official_ratio", "official_cash") else 0


def merge_evidence(results: list[dict]) -> list[dict]:
    """合并历史快照: 官方结论优先, 后续临时失败不覆盖已核验结果."""
    best: dict[tuple[str, str], dict] = {}
    for path in sorted(OUT_DIR.glob("equity_event_verification_*.json")):
        if path.name == CURATED_PATH.name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for item in data.get("results", []):
            key = (str(item.get("symbol")), str(item.get("event_date")))
            current = best.get(key)
            if current is None or (
                _status_rank(item.get("status")) > _status_rank(current.get("status"))
            ):
                best[key] = item
    for item in results:
        key = (str(item["symbol"]), str(item["event_date"]))
        current = best.get(key)
        if current is None or (
            _status_rank(item["status"]) >= _status_rank(current.get("status"))
        ):
            best[key] = item
    return sorted(
        best.values(),
        key=lambda row: (str(row.get("event_date")), str(row.get("symbol"))),
    )


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="权益事件官方公告核验")
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    wanted = {str(s).zfill(6) for s in args.symbols} if args.symbols else None
    events = [
        e for e in (CURATED_EVENTS + EXTRA_EVENTS)
        if wanted is None or str(e["symbol"]).zfill(6) in wanted
    ]
    session = _session()
    ann_cache: dict[str, list[dict]] = {}
    results = []

    for event in events:
        symbol = str(event["symbol"]).zfill(6)
        event_date = event["event_date"]
        if symbol not in ann_cache:
            try:
                ann_cache[symbol] = fetch_announcements(session, symbol)
            except Exception as exc:  # noqa: BLE001
                ann_cache[symbol] = []
                print(f"[WARN] {symbol} 公告列表失败: {type(exc).__name__}: {exc}")
        announcements = ann_cache[symbol]
        candidate = pick_candidate(announcements, event_date)
        item = {
            "symbol": symbol,
            "event_date": event_date.isoformat(),
            "inferred_ratio": float(event["ratio"]),
            "note": event.get("note"),
            "announcement": candidate,
            "official_ratio": None,
            "cash_per_share": None,
            "status": "unresolved",
        }
        override = OFFICIAL_OVERRIDES.get((symbol, event_date))
        if override:
            item.update(override)
            item["status"] = "official_ratio"
            item["source"] = override.get("source", "fund_company_pdf")
        elif candidate:
            try:
                text = fetch_content(session, candidate["id"])
                extracted = extract_official(text)
                item.update(extracted)
                if extracted["official_ratio"] or extracted["cash_per_share"]:
                    item["status"] = (
                        "official_ratio" if extracted["official_ratio"] else "official_cash"
                    )
                item["content_chars"] = len(text)
            except Exception as exc:  # noqa: BLE001
                item["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        results.append(item)
        print(
            f"{symbol} {event_date} inferred={event['ratio']:.4f} "
            f"official={item['official_ratio']} cash={item['cash_per_share']} "
            f"status={item['status']}"
        )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.output:
        output = Path(args.output)
    else:
        output = OUT_DIR / f"equity_event_verification_{stamp}.json"
    output.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "source": "eastmoney fund announcement feed (type=2) + content API",
                "results": results,
            },
            ensure_ascii=False, indent=2, default=str,
        ),
        encoding="utf-8",
    )
    merged = merge_evidence(results)
    CURATED_PATH.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "source": "merged evidence (official results preferred)",
                "results": merged,
            },
            ensure_ascii=False, indent=2, default=str,
        ),
        encoding="utf-8",
    )
    print(f"\n输出: {output}")
    print(f"合并证据: {CURATED_PATH} ({len(merged)} 条)")


if __name__ == "__main__":
    main()
