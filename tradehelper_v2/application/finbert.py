"""惰性加载的本地 FinBERT；模型不可用时保留新闻缺失状态，不猜测情绪。"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from hashlib import sha256
from typing import Callable
from tradehelper_v2.contracts.market_data import NewsSnapshot

@dataclass(frozen=True, slots=True)
class FinbertStatus:
    available: bool
    loaded: bool
    model_version: str | None
    reason: str | None

class FinbertEnricher:
    def __init__(self, model_path: Path | None, *, model_loader: Callable | None=None):
        self.model_path=model_path; self.model_loader=model_loader; self._pipeline=None; self._attempted=False; self._reason=None
    @property
    def available(self): return self.model_loader is not None or (self.model_path is not None and self.model_path.exists())
    @property
    def status(self): return FinbertStatus(self.available,self._pipeline is not None,str(self.model_path) if self._pipeline else None,self._reason)
    def model_hash(self) -> str | None:
        """模型目录的稳定内容哈希，写入 enrichment 审计而不写入新闻事实身份。"""
        if self.model_path is None or not self.model_path.exists(): return None
        digest=sha256()
        paths=(self.model_path,) if self.model_path.is_file() else tuple(sorted(item for item in self.model_path.rglob("*") if item.is_file()))
        for item in paths:
            digest.update(str(item.relative_to(self.model_path) if self.model_path.is_dir() else item.name).encode()); digest.update(item.read_bytes())
        return digest.hexdigest()
    def _load(self):
        if self._attempted: return self._pipeline
        self._attempted=True
        if not self.available: self._reason="FINBERT_MODEL_UNAVAILABLE"; return None
        try:
            if self.model_loader: self._pipeline=self.model_loader(self.model_path)
            else:
                from transformers import pipeline
                self._pipeline=pipeline("text-classification",model=str(self.model_path),tokenizer=str(self.model_path))
        except Exception as exc: self._reason=f"FINBERT_LOAD_FAILED:{type(exc).__name__}"; self._pipeline=None
        return self._pipeline
    def enrich(self, items):
        pipe=self._load()
        if pipe is None: return tuple(items)
        result=[]
        for item in items:
            if item.finbert_label is not None and item.finbert_score is not None: result.append(item); continue
            try:
                value=pipe((item.title+" "+(item.content or "")).strip())[0]
                raw_label=str(value.get("label","unknown")).lower()
                label={"label_0":"negative","label_1":"neutral","label_2":"positive","neg":"negative","neu":"neutral","pos":"positive"}.get(raw_label,raw_label)
                if label not in {"positive","neutral","negative"}: raise ValueError("FINBERT_UNKNOWN_LABEL")
                score=float(value.get("score",0.0))
                result.append(NewsSnapshot(item.instrument,item.title,item.source,item.published_at,item.available_at,item.fetched_at,item.content,item.is_macro,label,score,item.relevance,item.schema_version))
            except Exception:
                result.append(item)
        return tuple(result)
