from forecast.feature_sets import (
    REVERSION_CORE_V1,
    TECHNICAL_CORE_V1,
    TREND_CORE_V1,
    feature_names,
)


def test_fc06_feature_allowlist_excludes_current_absolute_ma_and_text() -> None:
    names = feature_names("tech")
    assert names == TECHNICAL_CORE_V1
    assert all(not item.startswith("current.") for item in names)
    assert "closed.ma_5" not in names and "closed.ma_distance_5" in names


def test_compact_domain_feature_sets_are_fixed_technical_subsets() -> None:
    assert feature_names("trend") == TREND_CORE_V1
    assert feature_names("reversion") == REVERSION_CORE_V1
    assert set(TREND_CORE_V1) < set(TECHNICAL_CORE_V1)
    assert set(REVERSION_CORE_V1) < set(TECHNICAL_CORE_V1)
