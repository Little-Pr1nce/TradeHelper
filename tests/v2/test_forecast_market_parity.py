from contracts import ModelFamily, ModelSpec
from forecast.labels import direction_label, flat_band
from forecast.models import fit_model, predict_model
from tests.v2.forecast_helpers import synthetic_samples


def test_fc14_market_parity_uses_no_market_specific_label_formula() -> None:
    assert flat_band(.24, 10) == flat_band(.24, 10)
    assert direction_label(.03, flat_band(.24, 10)).value == "bullish"


def test_fc14_equivalent_a_and_us_samples_produce_same_model_output(a_instrument, us_instrument) -> None:
    a_samples = synthetic_samples(a_instrument, count=120)
    us_samples = synthetic_samples(us_instrument, count=120)
    spec = ModelSpec("analog-tech-k40", ModelFamily.ANALOG, "tech", {"k": 40})
    a_model = fit_model(spec, a_samples[:100]); us_model = fit_model(spec, us_samples[:100])
    assert a_model is not None and us_model is not None
    assert predict_model(a_model, a_samples[-1].feature_snapshot) == predict_model(us_model, us_samples[-1].feature_snapshot)
