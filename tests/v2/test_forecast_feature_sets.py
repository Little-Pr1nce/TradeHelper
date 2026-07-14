from tradehelper_v2.forecast.feature_sets import TECHNICAL_CORE_V1, feature_names


def test_fc06_feature_allowlist_excludes_current_absolute_ma_and_text() -> None:
    names = feature_names("tech")
    assert names == TECHNICAL_CORE_V1
    assert all(not item.startswith("current.") for item in names)
    assert "closed.ma_5" not in names and "closed.ma_distance_5" in names
