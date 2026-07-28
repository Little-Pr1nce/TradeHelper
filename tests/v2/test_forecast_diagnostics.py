from contracts import DirectionProbabilities, ForecastDirection, ReturnDistribution
from forecast.diagnostics import evaluate_predictions, paired_brier_improvement_interval


def test_fc09_diagnostics_are_deterministic_and_calibrated_arrays_align() -> None:
    labels = (ForecastDirection.BULLISH, ForecastDirection.BEARISH) * 20
    probabilities = tuple(DirectionProbabilities(.8, .1, .1) if label is ForecastDirection.BULLISH else DirectionProbabilities(.1, .1, .8) for label in labels)
    intervals = tuple(ReturnDistribution(-.05, 0., .05, "empirical") for _ in labels)
    metrics = evaluate_predictions(probabilities, labels, tuple(.01 for _ in labels), intervals, horizon=5, seed=7)
    assert metrics.sample_count == 40 and metrics.accuracy == 1.0
    assert paired_brier_improvement_interval(probabilities, probabilities, labels, horizon=5, seed=7)[0] == 0.0
