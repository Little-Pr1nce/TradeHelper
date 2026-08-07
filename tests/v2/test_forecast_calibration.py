from contracts import DirectionProbabilities, ForecastDirection
from forecast.diagnostics import fit_prior_shrinkage, log_loss


def test_prior_shrinkage_corrects_recent_class_bias_without_future_data() -> None:
    probabilities = tuple(DirectionProbabilities(0.65, 0.2, 0.15) for _ in range(60))
    labels = tuple(
        ForecastDirection.BEARISH if index < 36 else ForecastDirection.NEUTRAL
        for index in range(60)
    )
    prior_labels = tuple(ForecastDirection.BEARISH for _ in range(90))
    shrinkage, prior = fit_prior_shrinkage(probabilities, labels, prior_labels=prior_labels)
    adjusted = tuple(
        DirectionProbabilities(
            item.bullish * (1 - shrinkage) + prior.bullish * shrinkage,
            item.neutral * (1 - shrinkage) + prior.neutral * shrinkage,
            item.bearish * (1 - shrinkage) + prior.bearish * shrinkage,
        )
        for item in probabilities
    )
    assert 0 < shrinkage <= 0.6
    assert log_loss(adjusted, labels) < log_loss(probabilities, labels)
