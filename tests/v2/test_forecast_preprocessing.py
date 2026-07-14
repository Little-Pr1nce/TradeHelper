from tradehelper_v2.forecast.preprocessing import RobustMissingPreprocessor


def test_fc05_preprocessor_is_fit_only_on_training_rows() -> None:
    names = ("a", "b", "c", "d", "e")
    fitted = RobustMissingPreprocessor.fit(names, ((1, 1, 1, 1, 1), (3, None, 3, 3, 3)))
    assert fitted is not None
    transformed = fitted.transform(((10_000, None, 1, 1, 1),))
    assert transformed.shape == (1, 10)
    assert transformed[0, 5 + 1] == 1.0
    assert fitted.medians[0] == 2.0
