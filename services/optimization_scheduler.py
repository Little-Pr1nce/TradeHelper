"""低优先级深度参数优化调度器。

前台报告只运行正式参数；参数池扩展和 walk-forward 在报告返回后串行执行，
避免多个股票同时占满 CPU，也避免同一数据截止日重复优化。
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="strategy-optimizer")
_inflight: set[tuple[str, str]] = set()
_forecast_inflight: set[tuple[str, str]] = set()
_lock = threading.Lock()


def schedule_forecast_optimization(
    *, stock_code: str, market: str, df: pd.DataFrame,
) -> bool:
    """预测优化独立调度，不依赖策略深度优化是否命中缓存。"""
    if not stock_code or df is None or df.empty:
        return False
    data_end = str(df["date"].iloc[-1])[:10] if "date" in df.columns else ""
    job_key = (stock_code.upper(), data_end)
    from data.database import Database
    from core.joint_oof import POLICY_VERSION
    db = Database()
    row = db.execute(
        """SELECT COUNT(*) n FROM forecast_model_versions
           WHERE stock_code=? AND train_end=?""",
        (stock_code.upper(), data_end),
    ).fetchone()
    if (
        row and int(row["n"] or 0) >= 3
        and db.has_joint_oof_run(stock_code, data_end, POLICY_VERSION)
    ):
        return False
    with _lock:
        if job_key in _forecast_inflight:
            return False
        _forecast_inflight.add(job_key)
    frame = df.copy(deep=True)

    def _run_forecast():
        try:
            from services.forecast_service import optimize_forecast_models
            result = optimize_forecast_models(
                Database(), df=frame, market=market, stock_code=stock_code,
            )
            logger.info(
                "后台预测优化完成: %s, 评估%d组, 晋升周期=%s",
                stock_code, result["evaluated"], result["promoted"],
            )
        except Exception as exc:
            logger.warning(f"后台预测优化失败 {stock_code}: {exc}", exc_info=True)
        finally:
            with _lock:
                _forecast_inflight.discard(job_key)

    _executor.submit(_run_forecast)
    return True


def schedule_deep_optimization(
    *,
    stock_code: str,
    market: str,
    df: pd.DataFrame,
    strategy_keys: list[str],
    initial_capital: float,
    news_df: pd.DataFrame | None = None,
) -> bool:
    """必要时提交后台深度优化；返回是否成功提交。"""
    if not stock_code or df is None or df.empty:
        return False
    schedule_forecast_optimization(stock_code=stock_code, market=market, df=df)
    if not strategy_keys:
        return False
    data_end = str(df["date"].iloc[-1])[:10] if "date" in df.columns else ""
    job_key = (stock_code, data_end)

    from data.database import Database
    db = Database()
    if db.has_recent_deep_optimization(stock_code, data_end):
        logger.info(f"后台深度优化缓存命中: {stock_code} {data_end}")
        return False

    with _lock:
        if job_key in _inflight:
            return False
        _inflight.add(job_key)
    db.mark_deep_optimization_started(stock_code, data_end)

    frame = df.copy(deep=True)
    news = news_df.copy(deep=True) if news_df is not None else None
    keys = list(dict.fromkeys(strategy_keys))

    def _run():
        try:
            from core.strategy_pool import expand_and_audit
            logger.info(f"后台深度优化开始: {stock_code} {data_end}")
            result = expand_and_audit(
                df=frame,
                strategy_keys=keys,
                market=market,
                stock_code=stock_code,
                initial_capital=initial_capital,
                news_df=news,
                db=Database(),
            )
            logger.info(
                f"后台深度优化完成: {stock_code}, "
                f"回测{result.total_backtests}组, PASS={len(result.pass_variants)}"
            )
            Database().mark_deep_optimization_finished(
                stock_code, data_end, variant_count=result.total_backtests
            )
        except Exception as exc:
            logger.warning(f"后台深度优化失败 {stock_code}: {exc}", exc_info=True)
            try:
                Database().mark_deep_optimization_finished(
                    stock_code, data_end, error=str(exc)
                )
            except Exception:
                pass
        finally:
            with _lock:
                _inflight.discard(job_key)

    _executor.submit(_run)
    return True
