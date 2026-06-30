"""
打分模型单元测试。

验证 compute_technical_normalized、align_finbert_scores、
calc_final_score 的纯函数特性和数学正确性。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import numpy as np

from alpha.scoring import (
    compute_technical_normalized,
    align_finbert_scores,
    calc_final_score,
    INDICATOR_COLUMNS,
)
from alpha.validation import apply_factor_weights, factor_validation_coverage


def assert_raises(exc_type, match=None):
    """简易 pytest.raises 替代。"""
    class _Ctx:
        def __enter__(self):
            return self
        def __exit__(self, exc, val, tb):
            if exc is None:
                raise AssertionError(f"期望抛出 {exc_type.__name__} 但未抛出")
            if not issubclass(exc, exc_type):
                return False
            if match and match not in str(val):
                raise AssertionError(f"异常信息 '{val}' 不匹配 '{match}'")
            return True
    return _Ctx()


def make_test_df(n: int = 200) -> pd.DataFrame:
    """构造包含 7 个技术指标的测试 DataFrame。"""
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    close = 100 + np.cumsum(np.random.randn(n) * 2)
    df = pd.DataFrame({
        "date": dates,
        "open": close + np.random.randn(n) * 0.5,
        "high": close + np.abs(np.random.randn(n) * 1),
        "low": close - np.abs(np.random.randn(n) * 1),
        "close": close,
        "volume": np.random.randint(1000000, 10000000, n),
        "rsi": 50 + np.random.randn(n) * 15,
        "dif": np.random.randn(n) * 0.5,
        "macd_bar": np.random.randn(n) * 0.3,
        "bb_pct": 0.5 + np.random.randn(n) * 0.2,
        "k": 50 + np.random.randn(n) * 20,
        "d": 50 + np.random.randn(n) * 15,
        "j": 50 + np.random.randn(n) * 25,
    })
    return df


class TestComputeTechnicalNormalized:
    """测试技术面归一化函数。"""

    def test_output_range(self):
        """验证 Tech_Normalized_Score 在合理的 [-1, 1] 范围内。"""
        df = make_test_df(200)
        result = compute_technical_normalized(df)

        assert "Tech_Normalized_Score" in result.columns

        # 后半段（有足够滚动窗口）应该在 [-1, 1] 内
        valid = result["Tech_Normalized_Score"].dropna()
        assert len(valid) > 0
        assert valid.min() >= -1.0
        assert valid.max() <= 1.0

    def test_pure_function(self):
        """验证纯函数特性：相同输入应得相同输出。"""
        df = make_test_df(100)
        r1 = compute_technical_normalized(df)
        r2 = compute_technical_normalized(df)
        pd.testing.assert_frame_equal(r1, r2)

    def test_handles_missing_indicators(self):
        """验证缺失指标列时不会崩溃。"""
        df = pd.DataFrame({"close": [100, 101, 102]})
        result = compute_technical_normalized(df)
        assert "Tech_Normalized_Score" in result.columns
        # 所有值为 0（无可用指标）
        assert (result["Tech_Normalized_Score"] == 0.0).all()

    def test_empty_df(self):
        df = pd.DataFrame()
        result = compute_technical_normalized(df)
        assert "Tech_Normalized_Score" in result.columns

    def test_tanh_mapping(self):
        """验证 tanh 映射确实压缩了极端值。"""
        df = make_test_df(300)
        result = compute_technical_normalized(df)
        valid = result["Tech_Normalized_Score"].dropna()
        # tanh 的渐进线在 ±1，实际值应严格在 (-1, 1) 内
        assert valid.abs().max() < 1.0


class TestAlignFinbertScores:
    """测试 FinBERT 得分对齐函数。"""

    def test_exact_date_match(self):
        price_df = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=5, freq="B"),
            "close": [100, 101, 102, 103, 104],
        })
        news_df = pd.DataFrame({
            "date": pd.to_datetime(["2024-01-01", "2024-01-03", "2024-01-05"]),
            "finbert_score": [0.8, -0.5, 0.3],
        })

        result = align_finbert_scores(price_df, news_df)
        expected = pd.Series([0.8, 0.4, -0.5, -0.25, 0.3], name="FinBERT_Score")
        pd.testing.assert_series_equal(result.reset_index(drop=True), expected)

    def test_news_residue_decays_by_half_life(self):
        """验证无新闻日按半衰期递减，不会无限保持原得分。"""
        price_df = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=10, freq="B"),
            "close": range(100, 110),
        })
        news_df = pd.DataFrame({
            "date": pd.to_datetime(["2024-01-01"]),
            "finbert_score": [0.9],
        })

        result = align_finbert_scores(price_df, news_df)
        assert result.iloc[0] == 0.9
        assert result.iloc[1] == 0.45
        assert result.iloc[-1] < 0.01

    def test_empty_news(self):
        price_df = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=5, freq="B")})
        result = align_finbert_scores(price_df, None)
        assert (result == 0.0).all()
        assert len(result) == 5

    def test_all_zero_when_empty_news_df(self):
        price_df = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=3, freq="B")})
        news_df = pd.DataFrame()
        result = align_finbert_scores(price_df, news_df)
        assert (result == 0.0).all()


class TestCalcFinalScore:
    """测试最终 Alpha 信号合成函数。"""

    def test_weight_constraint(self):
        """验证权重不满足约束时抛异常。"""
        df = make_test_df(100)
        with assert_raises(ValueError, match="权重约束不满足"):
            calc_final_score(df, w_tech=0.5, w_news=0.3)

    def test_output_range(self):
        """验证 Final_Score 严格在 [-1, 1] 内。"""
        df = make_test_df(200)
        result = calc_final_score(df)
        valid = result["Final_Score"].dropna()
        assert valid.min() >= -1.0
        assert valid.max() <= 1.0

    def test_pure_function(self):
        """验证纯函数特性。"""
        df = make_test_df(100)
        r1 = calc_final_score(df)
        r2 = calc_final_score(df)
        pd.testing.assert_frame_equal(r1, r2)

    def test_default_weights(self):
        df = make_test_df(200)
        result = calc_final_score(df)
        assert "Final_Score" in result.columns
        assert "FinBERT_Score" in result.columns
        assert "Tech_Normalized_Score" in result.columns

    def test_with_news(self):
        """验证含新闻数据的合成。"""
        df = make_test_df(100)
        news_df = pd.DataFrame({
            "date": df["date"].iloc[::10],
            "finbert_score": [0.5] * 10,
        })
        result = calc_final_score(df, news_df, w_tech=0.5, w_news=0.5)
        # 有新闻的日期使用原得分，之后只保留衰减后的残留。
        news_dates = set(news_df["date"].dt.strftime("%Y-%m-%d"))
        df_dates = df["date"].dt.strftime("%Y-%m-%d")
        for i, d in enumerate(df_dates):
            if d in news_dates:
                assert result["FinBERT_Score"].iloc[i] == 0.5
        assert ((result["FinBERT_Score"] >= 0) & (result["FinBERT_Score"] <= 0.5)).all()

    def test_reliability_only_shrinks_latest_signal(self):
        df = make_test_df(120)
        full = calc_final_score(df, prediction_reliability=1.0)
        reduced = calc_final_score(df, prediction_reliability=0.3)

        pd.testing.assert_series_equal(
            full["Final_Score"].iloc[:-1], reduced["Final_Score"].iloc[:-1]
        )
        assert np.isclose(
            reduced["Final_Score"].iloc[-1], full["Final_Score"].iloc[-1] * 0.3
        )


def test_factor_reliability_does_not_cancel_during_normalization():
    values = {
        "rsi": pd.Series([0.2, 0.4]),
        "dif": pd.Series([0.0, 0.6]),
    }
    validation = {
        key: {"multiplier": 1.0, "direction_correct": True}
        for key in values
    }
    full = apply_factor_weights(values, validation, prediction_reliability=1.0)
    reduced = apply_factor_weights(values, validation, prediction_reliability=0.3)
    pd.testing.assert_series_equal(reduced, full * 0.3)


def test_unknown_factor_keeps_prior_weight_without_direction_penalty():
    values = {
        "rsi": pd.Series([1.0]),
        "dif": pd.Series([-1.0]),
    }
    validation = {
        "rsi": {"grade": "?", "multiplier": 1.0, "direction_correct": None},
        "dif": {"grade": "A", "multiplier": 1.0, "direction_correct": True},
    }
    result = apply_factor_weights(
        values, validation, regime_weights={"rsi": 0.8, "dif": 0.2}
    )
    assert np.isclose(result.iloc[0], 0.6)
    assert factor_validation_coverage(validation) == 0.5


def test_future_prices_cannot_rewrite_past_factor_scores():
    base = make_test_df(220)
    changed = base.copy()
    changed.loc[changed.index[160]:, "close"] = np.linspace(200, 80, 60)

    original = compute_technical_normalized(base, validate=True)
    mutated = compute_technical_normalized(changed, validate=True)

    pd.testing.assert_series_equal(
        original["Tech_Normalized_Score"].iloc[:160],
        mutated["Tech_Normalized_Score"].iloc[:160],
    )


def _run_script_tests():
    import inspect

    current_module = sys.modules[__name__]
    total = 0
    failures = []
    for name, obj in list(vars(current_module).items()):
        if callable(obj) and name.startswith("test_"):
            total += 1
            try:
                obj()
            except Exception as exc:
                failures.append((name, exc))
        if inspect.isclass(obj) and name.startswith("Test"):
            for method_name, _ in inspect.getmembers(obj, inspect.isfunction):
                if not method_name.startswith("test_"):
                    continue
                total += 1
                try:
                    getattr(obj(), method_name)()
                except Exception as exc:
                    failures.append((f"{name}.{method_name}", exc))
    if failures:
        for test_name, exc in failures:
            print(f"FAIL {test_name}: {exc}")
        raise SystemExit(1)
    print(f"{total}/{total} passed")


if __name__ == "__main__":
    _run_script_tests()
