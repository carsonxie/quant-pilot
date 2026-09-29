"""基准运行器 (M5) — 在固定研究配置上执行 B0/B1 并留痕实验记录.

契约 (spec §5.1/§7.1, plan §M5):
  - 固定研究配置: 权重/资本/带宽来自 settings.baselines, 一次记录, 不搜索最优 SAA.
  - 同口径: B0/B1 共用相同初始映射/资本/产品范围/价格/成本框架与同一账户账本路径.
  - 实验记录: experiments 表保存 代码/数据/池/配置/成本情景/结果位置/状态,
    失败试验同样保留 (P05-D6); 结论可为"完成, 结论见结果"。
  - 结果位置: data/backtests/ 下 Parquet (NAV 序列, 可由事件重建) + 摘要 json。
  - 成本情景命名 (spec §6.3): 运行使用 settings.trading_cost.scenario (当前
    legacy_stress, 旧压力情景); 更不利情景通过覆盖 trading_cost 字段实验对照。

Usage:
    uv run baseline --strategy B0 --start 2020-01-01 --end 2026-09-11
    uv run baseline --strategy B1 --start 2020-01-01 --end 2026-09-11
    uv run baseline --strategy B0 --end 2026-09-11 --write-account   # 落库模拟账户
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import polars as pl

from backtest.baseline import (
    CALIBER_NOTE,
    BaselineConfig,
    BaselineResult,
    BaselineSimulator,
)
from data_pipeline.duckdb_manager import DuckDBManager

logger = logging.getLogger(__name__)


def run_baseline(
    settings: dict,
    db_path: str,
    strategy: str,
    start: date,
    end: date,
    *,
    deposits: list[tuple[date, float]] | None = None,
    write_account: bool = False,
    result_dir: str | Path | None = None,
) -> BaselineResult:
    """执行一次基准模拟并记录实验 (代码/数据/配置/成本/结果位置).

    Args:
        settings: settings.yaml (baselines / trading_cost / project)
        db_path: DuckDB 路径
        strategy: B0 / B1
        start/end: 模拟区间
        deposits: 外部现金流 [(日期, 金额)] (预先声明; 同额同日)
        write_account: True 时把模拟账户与事件写入数据库 (默认只算不写)
        result_dir: 结果目录 (缺省 data/backtests)
    """
    strategy = strategy.upper()
    if strategy not in ("B0", "B1"):
        raise ValueError(f"未知基准策略: {strategy} (支持 B0/B1)")
    config = BaselineConfig.from_settings(settings, strategy)
    project = settings.get("project", {})
    cost_cfg = settings.get("trading_cost", {})
    cost_scenario = str(cost_cfg.get("scenario", "unversioned"))

    experiment_id = (
        f"baseline-{strategy}-{start:%Y%m%d}-{end:%Y%m%d}-"
        f"{config.config_fingerprint()}-{cost_scenario}"
    )
    result_dir = Path(result_dir) if result_dir else Path("data/backtests")
    result_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now()
    mode = str(project.get("mode", "research"))

    db = DuckDBManager(db_path)
    db.init_database()
    try:
        # 池版本: 若存在 instruments 记录, 取最新声明的标的集哈希 (同口径留痕)
        pool_version = None
        try:
            db.get_latest_universe_version()
            versions = db.conn.execute(
                "SELECT version_id FROM universe_versions ORDER BY as_of DESC LIMIT 1"
            ).fetchone()
            pool_version = versions[0] if versions else None
        except Exception:
            pool_version = None

        try:
            simulator = BaselineSimulator(
                db, settings, config, mode="backtest", live_write=write_account,
            )
            result = simulator.run(start, end, deposits=deposits)
        except Exception as exc:
            # 失败试验同样保留 (spec §7.1: 失败不删除; P05-D6)
            _record_failed_experiment(
                db, experiment_id, strategy, config, project, cost_scenario,
                start, end, started_at, exc,
            )
            raise
        result.experiment_id = experiment_id
        result.notes.insert(
            0,
            f"研究示例配置 {config.label} (非真实 SAA; spec §5.1)",
        )

        nav_path = result_dir / f"{experiment_id}.parquet"
        if not result.nav.is_empty():
            result.nav.write_parquet(nav_path)
        summary_path = result_dir / f"{experiment_id}.json"
        summary = {
            "experiment_id": experiment_id,
            "strategy": strategy,
            "account_id": result.account_id,
            "config_fingerprint": config.config_fingerprint(),
            "weights": config.weights,
            "initial_cash": config.initial_cash,
            "band_points": config.band_points,
            "period": {"start": str(start), "end": str(end)},
            "period_return": result.period_return(),
            "max_drawdown": result.max_drawdown(),
            "trades": result.trades,
            "fees_total": result.fees_total,
            "cash_flow_policy": result.cash_flow_policy,
            "cost_scenario": cost_scenario,
            "caliber_note": CALIBER_NOTE,
            "wrote_account": write_account,
            "nav_rows": len(result.nav),
            "notes": result.notes,
        }
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
        )

        db.upsert_experiment(pl.DataFrame([{
            "experiment_id": experiment_id,
            "kind": f"baseline_{strategy.lower()}",
            "code_version": str(project.get("strategy_version", "unversioned")),
            "data_version": f"{result.nav['date'][-1] if not result.nav.is_empty() else end}",
            "pool_version": pool_version,
            "config_json": json.dumps(
                {
                    "weights": config.weights, "initial_cash": config.initial_cash,
                    "band_points": config.band_points, "lot_size": config.lot_size,
                    "check_weekday": config.check_weekday, "label": config.label,
                    "cost_scenario": cost_scenario,
                    "deposits": [
                        {"date": str(d), "amount": a} for d, a in (deposits or [])
                    ],
                },
                ensure_ascii=False,
            ),
            "cost_scenario": cost_scenario,
            "purpose": "固定研究配置基准 (B0 买入持有 / B1 阈值再平衡); 不搜索最优 SAA",
            "result_location": str(nav_path),
            "status": "ok",
            "conclusion": (
                f"完成: 期间收益 {result.period_return():.2%}, "
                f"最大回撤 {result.max_drawdown():.2%}, 成交 {result.trades} 笔"
            ),
            "mode": mode,
            "started_at": started_at,
            "finished_at": datetime.now(),
        }]))
        logger.info("实验记录写入: %s (结果 %s)", experiment_id, nav_path)
        return result
    finally:
        db.close()


def _record_failed_experiment(
    db: DuckDBManager,
    experiment_id: str,
    strategy: str,
    config: BaselineConfig,
    project: dict,
    cost_scenario: str,
    start: date,
    end: date,
    started_at: datetime,
    exc: Exception,
) -> None:
    """失败试验留痕 (失败不删除, 状态可区分; spec §7.1/P05-D6)."""
    try:
        db.upsert_experiment(pl.DataFrame([{
            "experiment_id": experiment_id,
            "kind": f"baseline_{strategy.lower()}",
            "code_version": str(project.get("strategy_version", "unversioned")),
            "data_version": str(end),
            "cost_scenario": cost_scenario,
            "purpose": "固定研究配置基准 (B0/B1); 失败试验保留",
            "status": "failed",
            "conclusion": f"运行失败: {type(exc).__name__}: {exc}"[:500],
            "mode": str(project.get("mode", "research")),
            "started_at": started_at,
            "finished_at": datetime.now(),
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }]))
        logger.warning("失败实验已留痕: %s (%s)", experiment_id, exc)
    except Exception:
        logger.exception("失败实验留痕失败 (不影响原始错误)")


# ------------------------------------------------------------------
# CLI (uv run baseline)
# ------------------------------------------------------------------

def main() -> None:
    import argparse

    import yaml

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    project_root = Path(__file__).resolve().parents[2]
    with open(project_root / "config" / "settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    parser = argparse.ArgumentParser(
        description="Quant-Pilot 基准模拟 (B0 买入持有 / B1 阈值再平衡; 研究示例配置)",
    )
    parser.add_argument("--strategy", required=True, choices=["B0", "B1", "b0", "b1"])
    parser.add_argument("--start", default=None, help="起始日 YYYY-MM-DD (缺省=数据起点)")
    parser.add_argument("--end", default=None, help="结束日 YYYY-MM-DD (缺省=数据截止)")
    parser.add_argument(
        "--db", default=None, help="DuckDB 路径 (缺省读 settings)",
    )
    parser.add_argument(
        "--write-account", action="store_true",
        help="将模拟账户与事件写入数据库 (默认只计算并记录实验)",
    )
    args = parser.parse_args()

    db_path = args.db or str(project_root / settings["storage"]["db_path"])
    db = DuckDBManager(db_path)
    try:
        min_date, max_date = db.get_date_range()
    finally:
        db.close()
    start = date.fromisoformat(args.start) if args.start else date.fromisoformat(str(min_date))
    end = date.fromisoformat(args.end) if args.end else date.fromisoformat(str(max_date))

    result = run_baseline(
        settings, db_path, args.strategy, start, end,
        write_account=args.write_account,
    )
    print(result.summary())


if __name__ == "__main__":
    main()
