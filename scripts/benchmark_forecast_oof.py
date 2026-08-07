"""Read-only benchmark for stock-specific forecast candidates on cached real bars."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

from application.analysis import RuntimeAnalysisPipeline
from contracts import ForecastScope, InstrumentId, Market
from data.calendar import ExchangeTradingCalendar
from data.repository import SQLiteRepository
from features import FeatureBuilder
from forecast.trainer import ForecastTrainer


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+", help="US ticker or six-digit A-share code")
    parser.add_argument("--market", choices=("US", "A"), default="US")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--horizons", nargs="+", type=int, default=(1, 3, 5, 10))
    parser.add_argument("--json-dir", type=Path, help="Optional directory for one summary file per symbol")
    parser.add_argument("--selection-lookback", type=int, default=360)
    parser.add_argument("--panel-symbols", nargs="*", default=())
    parser.add_argument("--workers", type=int, default=1)
    return parser.parse_args()


def _training_samples(repository: SQLiteRepository, instrument: InstrumentId) -> tuple:
    metadata = repository.get_stock_metadata(instrument)
    listing_date = metadata.listing_date if metadata is not None else None
    bars = repository.list_daily_bars(instrument, date(1990, 1, 1), date(2100, 1, 1))
    calendar = ExchangeTradingCalendar()
    pipeline = RuntimeAnalysisPipeline(
        SimpleNamespace(calendar=calendar, feature_builder=FeatureBuilder(calendar)),
    )
    samples = pipeline._technical_training_samples(
        SimpleNamespace(instrument=instrument, bars=bars, listing_date=listing_date),
    )
    return samples


def _benchmark(
    repository: SQLiteRepository, symbol: str, market: Market,
    horizons: tuple[int, ...], selection_lookback: int,
    panel_symbols: tuple[str, ...],
    samples_by_symbol: dict[str, tuple] | None = None,
) -> list[dict]:
    instrument = InstrumentId.from_code(symbol, market)
    requested = tuple(dict.fromkeys((symbol, *panel_symbols)))
    samples_by_symbol = samples_by_symbol or {
        item: _training_samples(repository, InstrumentId.from_code(item, market))
        for item in requested
    }
    samples = samples_by_symbol[symbol]
    panel_samples = tuple(sorted(
        (
            sample
            for panel_symbol in panel_symbols
            for sample in samples_by_symbol[panel_symbol]
        ),
        key=lambda sample: (
            sample.origin_session_date, sample.instrument.stable_key, sample.horizon,
        ),
    ))
    print(
        f"READY symbol={symbol} samples={len(samples)} panel_samples={len(panel_samples)}",
        flush=True,
    )
    trainer = ForecastTrainer(selection_lookback_dates=selection_lookback)
    summaries = []
    for horizon in horizons:
        print(f"START symbol={symbol} horizon={horizon}", flush=True)
        quarters: set[int] = set()

        def progress(_stage: str, completed: int, total: int) -> None:
            quarter = min(4, int(completed * 4 / total))
            if quarter not in quarters:
                quarters.add(quarter)
                print(
                    f"PROGRESS symbol={symbol} horizon={horizon} percent={quarter * 25}",
                    flush=True,
                )

        outcome = trainer.evaluate(
            samples, scope=ForecastScope.STOCK, scope_key=instrument.stable_key,
            horizon=horizon, progress=progress, panel_samples=panel_samples,
        )
        champion = outcome.champion.spec.spec_id if outcome.champion is not None else "none"
        print(
            f"RESULT symbol={symbol} horizon={horizon} status={outcome.status.value} champion={champion}",
            flush=True,
        )
        ranked = sorted(
            (item for item in outcome.evaluations if item.confirmation is not None),
            key=lambda item: item.confirmation.multiclass_brier,
        )
        for item in ranked[:7]:
            assert item.confirmation is not None and item.baseline_confirmation is not None
            print(
                "CANDIDATE "
                f"symbol={symbol} horizon={horizon} spec={item.spec.spec_id} "
                f"family={item.spec.family.value} status={item.status.value} "
                f"brier={item.confirmation.multiclass_brier:.4f} "
                f"baseline={item.baseline_confirmation.multiclass_brier:.4f} "
                f"accuracy={item.confirmation.accuracy:.3f} "
                f"log_loss={item.confirmation.log_loss:.4f} "
                f"baseline_log_loss={item.baseline_confirmation.log_loss:.4f} "
                f"ece={item.confirmation.expected_calibration_error:.4f} "
                f"baseline_ece={item.baseline_confirmation.expected_calibration_error:.4f} "
                f"coverage={item.confirmation.interval_coverage:.3f}",
                flush=True,
            )
        summaries.append({
            "symbol": symbol,
            "horizon": horizon,
            "status": outcome.status.value,
            "champion": champion,
            "top_confirmation": [
                {
                    "spec_id": item.spec.spec_id,
                    "family": item.spec.family.value,
                    "status": item.status.value,
                    "selection_brier": item.selection.multiclass_brier,
                    "selection_baseline_brier": item.baseline_selection.multiclass_brier,
                    "selection_log_loss": item.selection.log_loss,
                    "selection_ece": item.selection.expected_calibration_error,
                    "selection_interval_coverage": item.selection.interval_coverage,
                    "brier": item.confirmation.multiclass_brier,
                    "baseline_brier": item.baseline_confirmation.multiclass_brier,
                    "accuracy": item.confirmation.accuracy,
                    "log_loss": item.confirmation.log_loss,
                    "ece": item.confirmation.expected_calibration_error,
                    "interval_coverage": item.confirmation.interval_coverage,
                }
                for item in ranked
            ],
        })
    return summaries


def main() -> int:
    args = _arguments()
    repository = SQLiteRepository(args.database)
    try:
        market = Market(args.market)
        symbols = tuple(dict.fromkeys(item.upper() for item in args.symbols))
        panel_symbols = tuple(dict.fromkeys(item.upper() for item in args.panel_symbols))
        requested = tuple(dict.fromkeys((*symbols, *panel_symbols)))
        samples_by_symbol = {
            item: _training_samples(repository, InstrumentId.from_code(item, market))
            for item in requested
        }

        def run(normalized: str):
            return normalized, _benchmark(
                repository, normalized, market, tuple(args.horizons),
                args.selection_lookback, panel_symbols, samples_by_symbol,
            )

        workers = max(1, min(int(args.workers), len(symbols)))
        if workers == 1:
            results = [run(symbol) for symbol in symbols]
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="forecast-oof") as executor:
                futures = {executor.submit(run, symbol): symbol for symbol in symbols}
                results = [future.result() for future in as_completed(futures)]
        for normalized, summaries in sorted(results):
            if args.json_dir is not None:
                args.json_dir.mkdir(parents=True, exist_ok=True)
                (args.json_dir / f"{args.market}_{normalized}.json").write_text(
                    json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8",
                )
    finally:
        repository.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
