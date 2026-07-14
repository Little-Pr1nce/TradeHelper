"""仅使用训练折统计量的稳健缺失值预处理。

中位数/IQR 必须在每一个 OOF 训练折独立拟合。测试折和当前推理只可
复用对应模型 artifact 内的参数，不能为了“更准确”重新查看未来样本。
"""

from __future__ import annotations

from dataclasses import dataclass
import numpy as np


@dataclass(frozen=True, slots=True)
class RobustMissingPreprocessor:
    """按原始字段缩放并追加 is_missing 指示列的可序列化转换器。"""
    feature_names: tuple[str, ...]
    medians: tuple[float, ...]
    iqrs: tuple[float, ...]
    active_indices: tuple[int, ...]

    @classmethod
    def fit(cls, names: tuple[str, ...], rows: tuple[tuple[float | None, ...], ...]) -> "RobustMissingPreprocessor | None":
        """拟合训练折；全缺失字段移除，少于五个字段则拒绝本折。"""
        active: list[int] = []; medians: list[float] = []; iqrs: list[float] = []
        for index in range(len(names)):
            observed = np.array([row[index] for row in rows if row[index] is not None], dtype=float)
            if not len(observed):
                continue
            active.append(index); medians.append(float(np.median(observed))); iqrs.append(float(np.percentile(observed, 75) - np.percentile(observed, 25)))
        if len(active) < 5:
            return None
        return cls(names, tuple(medians), tuple(iqrs), tuple(active))

    def transform(self, rows: tuple[tuple[float | None, ...], ...]) -> np.ndarray:
        """转换但绝不修改 FeatureSnapshot 中记录的原始缺失事实。"""
        if not rows:
            return np.empty((0, len(self.active_indices) * 2), dtype=float)
        selected = np.asarray(
            [[np.nan if row[index] is None else float(row[index]) for index in self.active_indices] for row in rows],
            dtype=float,
        )
        medians = np.asarray(self.medians, dtype=float)
        iqrs = np.maximum(np.asarray(self.iqrs, dtype=float), 1e-12)
        missing = np.isnan(selected)
        filled = np.where(missing, medians, selected)
        scaled = np.clip((filled - medians) / iqrs, -8.0, 8.0)
        return np.concatenate((scaled, missing.astype(float)), axis=1)

    def to_dict(self) -> dict:
        return {"feature_names": self.feature_names, "medians": self.medians, "iqrs": self.iqrs, "active_indices": self.active_indices}

    @classmethod
    def from_dict(cls, payload: dict) -> "RobustMissingPreprocessor":
        return cls(
            tuple(payload["feature_names"]), tuple(float(value) for value in payload["medians"]),
            tuple(float(value) for value in payload["iqrs"]), tuple(int(value) for value in payload["active_indices"]),
        )
