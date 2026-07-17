"""双市场股票检索：本地元数据优先，受限外部 provider 兜底。"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping
from tradehelper_v2.contracts.enums import Exchange, Market
from tradehelper_v2.contracts.market_data import InstrumentId, StockMetadata

@dataclass(frozen=True, slots=True)
class InstrumentLookupResult:
    instrument: InstrumentId
    name: str
    source: str
    available_at: datetime

class InstrumentLookupService:
    def __init__(self, repository, search_provider: Callable | None = None, clock=None):
        self.repository=repository; self.search_provider=search_provider; self.clock=clock or (lambda:datetime.now(timezone.utc))
    @staticmethod
    def _canonical(market, query):
        m=market if isinstance(market,Market) else Market(str(market).upper()); code=str(query).strip().upper()
        if m is Market.A and code.isdigit() and len(code)==6: return InstrumentId.from_code(code,m)
        if m is Market.US and code and len(code)<=16 and any(ch.isalpha() for ch in code): return InstrumentId.from_code(code,m,Exchange.UNKNOWN)
        return None
    def lookup(self, market: Market | str, query: str, *, limit: int=20) -> tuple[InstrumentLookupResult,...]:
        m=market if isinstance(market,Market) else Market(str(market).upper()); q=str(query or "").strip()
        if not q: return ()
        now=self.clock(); output=[]
        exact=self._canonical(m,q)
        if exact:
            metadata=self.repository.get_stock_metadata(exact)
            if metadata is not None:
                output.append(InstrumentLookupResult(exact,metadata.name,"local",metadata.fetched_at))
        for metadata in self.repository.search_stock_metadata(m,q,limit=limit):
            output.append(InstrumentLookupResult(metadata.instrument,metadata.name,"local",metadata.fetched_at))
        if (not output or (exact is not None and not any(item.instrument == exact for item in output))) and self.search_provider is not None:
            values=self.search_provider(m,q)
            for value in values or ():
                try:
                    inst=value.instrument if hasattr(value,"instrument") else InstrumentId.from_code(value.get("code"),m,value.get("exchange"))
                    output.append(InstrumentLookupResult(inst,str(getattr(value,"name",None) or (value.get("name") if isinstance(value,Mapping) else inst.code)),"provider",now))
                except Exception: continue
        if exact is not None and not any(item.instrument == exact for item in output):
            output.append(InstrumentLookupResult(exact,exact.code,"canonical",now))
        unique={item.instrument.stable_key:item for item in output}
        needle_upper=q.upper()
        def ranking(item):
            code=item.instrument.code.upper(); name=item.name.upper()
            rank=0 if code==needle_upper else 1 if name.startswith(needle_upper) else 2 if needle_upper in name else 3
            return (rank,code,name,item.instrument.exchange.value)
        return tuple(sorted(unique.values(),key=ranking)[:max(1,min(20,limit))])
    __call__=lookup
