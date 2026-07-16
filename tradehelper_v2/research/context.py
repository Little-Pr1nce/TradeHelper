"""从调用方提供的冻结事实构建最小研究上下文，不联网、不读取数据库。"""
from __future__ import annotations

import re
from decimal import Decimal

from tradehelper_v2.contracts import ContractViolation, ResearchContext, ResearchFact, ResearchFactManifest, ResearchScope, stable_hash

MAX_NEWS_ITEMS_PER_INSTRUMENT = 10


class ResearchContextBuilder:
    @staticmethod
    def _fact(instrument,key,value,status,available_at,source_ref,source_payload_hash,unit=None,value_type=None):
        value_type=value_type or ("boolean" if isinstance(value,bool) else "number" if isinstance(value,(int,float,Decimal)) else "text")
        identity={"instrument":instrument,"key":key,"value":value,"status":status,"available_at":available_at,"source_refs":(source_ref,),"source_payload_hash":source_payload_hash}
        return ResearchFact(stable_hash(identity),instrument,key,value,value_type,unit,status,available_at,(source_ref,),source_payload_hash)

    @staticmethod
    def _safe_key(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9_]+", "_", value.strip().lower()).strip("_")
        return normalized or "field"

    @staticmethod
    def _summary(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized[:500] or None

    def project_upstream_facts(
        self,
        *,
        feature_snapshots=(),
        news_snapshots=(),
        fundamental_snapshots=(),
        forecasts=(),
        scenarios=(),
        strategy_bundles=(),
        risk_bundles=(),
        portfolio_bundles=(),
        learning_snapshots=(),
    ):
        """只从冻结 V2 合同字段投影事实，生产调用方不自行拼 ResearchFact。"""
        facts=[]
        for snapshot in feature_snapshots:
            source=snapshot.feature_hash
            for item in snapshot.values:
                facts.append(self._fact(snapshot.instrument,f"feature.{item.name}",item.value,item.status.value,item.available_at,source,source,item.unit))
        news_by_instrument={}
        for item in news_snapshots:
            news_by_instrument.setdefault(item.instrument,[]).append(item)
        for instrument,items in news_by_instrument.items():
            selected=sorted(items,key=lambda item:(item.available_at,item.published_at,item.stable_key),reverse=True)[:MAX_NEWS_ITEMS_PER_INSTRUMENT]
            for item in selected:
                hashed=stable_hash(item)
                suffix=hashed[:16]
                source_ref=f"news:{item.source}:{suffix}"
                values=(
                    ("title",item.title,None),
                    ("summary",self._summary(item.content),None),
                    ("sentiment_label",item.finbert_label,None),
                    ("sentiment_score",item.finbert_score,"ratio"),
                    ("first_available_at",item.available_at.isoformat(),None),
                )
                for name,value,unit in values:
                    facts.append(self._fact(instrument,f"feature.news.item.{suffix}.{name}",value,"available" if value is not None else "missing",item.available_at,source_ref,hashed,unit))
        for snapshot in fundamental_snapshots:
            snapshot_hash=stable_hash(snapshot)
            for raw_name,field in snapshot.fields.items():
                field_hash=stable_hash({"snapshot":snapshot_hash,"field":raw_name,"value":field})
                suffix=field_hash[:12]
                available_at=max(snapshot.available_at,field.published_at) if field.published_at else snapshot.available_at
                source_ref=f"fundamental:{snapshot_hash}:{raw_name}:{field.source}"
                key=f"feature.fund.raw.{self._safe_key(raw_name)}.{suffix}"
                facts.append(self._fact(snapshot.instrument,key,field.value,"available" if field.value is not None else "missing",available_at,source_ref,field_hash,field.unit))
        for item in forecasts:
            source=item.event_key; hashed=stable_hash(item); prefix=f"forecast.{item.horizon}"
            available=item.availability.value=="available"
            status="available" if available else "missing"
            values={
                "target_session_date":item.target_session_date.isoformat() if item.target_session_date else None,
                "direction":item.direction.value if item.direction else None,
                "probability_bullish":item.probabilities.bullish if item.probabilities else None,
                "probability_neutral":item.probabilities.neutral if item.probabilities else None,
                "probability_bearish":item.probabilities.bearish if item.probabilities else None,
                "return_p10":item.return_distribution.p10 if item.return_distribution else None,
                "return_p50":item.return_distribution.p50 if item.return_distribution else None,
                "return_p90":item.return_distribution.p90 if item.return_distribution else None,
                "validation_status":item.validation_status.value,
                "event_key":item.event_key,
            }
            for name,value in values.items():
                facts.append(self._fact(item.instrument,f"{prefix}.{name}",value,status if value is None else "available",item.cutoff_at,source,hashed,"ratio" if name.startswith(("probability_","return_")) else None))
        for item in scenarios:
            hashed=stable_hash(item)
            for name,value in (("state",item.state.value),("bias",item.bias.value),("status",item.status.value),("entry_posture",item.entry_posture.value),("exit_posture",item.exit_posture.value)):
                facts.append(self._fact(item.instrument,f"scenario.{name}",value,"available",item.as_of,item.scenario_id,hashed))
        for item in strategy_bundles:
            hashed=stable_hash(item)
            for name,value in (("position_state",item.position_state.value),("conflict_state",item.conflict_state),("bundle_id",item.bundle_id)):
                facts.append(self._fact(item.instrument,f"strategy.{item.bundle_id}.{name}",value,"available",item.generated_at,item.bundle_id,hashed))
            for branch in (item.entry_or_add,item.reduce_or_exit,item.hold,item.invalidation):
                for plan in branch.plans:
                    plan_hash=stable_hash(plan)
                    values=(
                        ("action",plan.action.value),
                        ("readiness",plan.readiness.value),
                        ("trigger_condition_id",plan.trigger_condition.condition_id),
                        ("confirmation_condition_id",plan.confirmation_condition.condition_id if plan.confirmation_condition else None),
                        ("stop_condition_id",plan.stop.condition.condition_id if plan.stop else None),
                        ("stop_mode",plan.stop.mode.value if plan.stop else None),
                        ("take_profit_condition_id",plan.take_profit.condition.condition_id if plan.take_profit and plan.take_profit.condition else None),
                        ("take_profit_mode",plan.take_profit.mode.value if plan.take_profit else None),
                        ("invalidation_condition_id",plan.invalidation_condition.condition_id),
                        ("valid_from",plan.valid_from.isoformat() if plan.valid_from else None),
                        ("expires_at",plan.expires_at.isoformat() if plan.expires_at else None),
                    )
                    for name,value in values:
                        facts.append(self._fact(item.instrument,f"strategy.{plan.plan_id}.{name}",value,"available" if value is not None else "missing",item.generated_at,plan.plan_id,plan_hash))
        for item in risk_bundles:
            hashed=stable_hash(item)
            facts.append(self._fact(item.instrument,f"risk.{item.risk_bundle_id}.decision_count",len(item.decisions),"available",item.generated_at,item.risk_bundle_id,hashed))
            facts.append(self._fact(item.instrument,f"risk.{item.risk_bundle_id}.levels",",".join(sorted(decision.level.value for decision in item.decisions)),"available",item.generated_at,item.risk_bundle_id,hashed))
            for decision in item.decisions:
                decision_hash=stable_hash(decision)
                values=(
                    ("level",decision.level.value,None),
                    ("disposition",decision.disposition.value,None),
                    ("executable_now",decision.executable_now,None),
                    ("recheck_at_trigger",decision.recheck_at_trigger,None),
                    ("current_position_pct",decision.current_position_pct,"ratio"),
                    ("post_trade_position_pct",decision.post_trade_position_pct,"ratio"),
                    ("reason_codes",",".join(decision.reason_codes),None),
                )
                for name,value,unit in values:
                    facts.append(self._fact(item.instrument,f"risk.{decision.decision_id}.{name}",value,"available" if value is not None else "missing",decision.generated_at,decision.decision_id,decision_hash,unit))
        for item in portfolio_bundles:
            hashed=stable_hash(item)
            facts.append(self._fact(None,f"portfolio.{item.portfolio_bundle_id}.market",item.market.value,"available",item.generated_at,item.portfolio_bundle_id,hashed))
        for item in learning_snapshots:
            hashed=stable_hash(item)
            for name,value in item.metrics:
                facts.append(self._fact(None,f"learning.{item.snapshot_id}.{name}",value,"available" if value is not None else "missing",item.generated_at,item.snapshot_id,hashed))
        return tuple(facts)

    def build_manifest(self, *, scope, market, cutoff_at, instruments, facts, artifact_refs=(), generated_at):
        instruments=tuple(sorted(set(instruments),key=lambda item:item.stable_key))
        instrument_set=set(instruments)
        if any(fact.instrument is not None and fact.instrument not in instrument_set for fact in facts):
            raise ContractViolation("research facts must belong to manifest instruments")
        by_key={}
        for fact in facts:
            key=(fact.instrument,fact.key)
            if key in by_key and by_key[key] != fact:
                # 冲突不能“挑一个较新/较可信”的值。保留可审计来源并投影为一个
                # conflicting 事实，后续 validator 必然给出 invalid_data。
                previous=by_key[key]
                sources=tuple(sorted(set(previous.source_refs+fact.source_refs)))
                available=max(previous.available_at,fact.available_at)
                source_hash=stable_hash(tuple(sorted(filter(None,(previous.source_payload_hash,fact.source_payload_hash)))))
                identity={"instrument":fact.instrument,"key":fact.key,"value":None,"status":"conflicting","available_at":available,"source_refs":sources,"source_payload_hash":source_hash}
                by_key[key]=ResearchFact(stable_hash(identity),fact.instrument,fact.key,None,fact.value_type,fact.unit,"conflicting",available,sources,source_hash)
                continue
            by_key[key]=fact
        ordered=tuple(sorted(by_key.values(),key=lambda item:item.fact_id))
        artifacts=tuple(sorted(set(artifact_refs) | {ref for item in ordered for ref in item.source_refs}))
        identity={"scope":scope,"market":market,"cutoff":cutoff_at,"instruments":instruments,"facts":ordered,"artifacts":artifacts,"schema":1}
        return ResearchFactManifest(stable_hash(identity),scope,market,cutoff_at,instruments,ordered,artifacts,1,generated_at)

    def build_context(self, *, scope, market, mode, cutoff_at, manifest, instrument_roles, forecast_event_keys=(), scenario_ids=(), strategy_bundle_ids=(), risk_bundle_ids=(), portfolio_bundle_id=None, learning_snapshot_ids=(), prompt_input_version="research_prompt_input_v1", generated_at):
        identity={"scope":scope,"market":market,"mode":mode,"cutoff":cutoff_at,"manifest":manifest.manifest_id,"roles":tuple(sorted(instrument_roles,key=lambda item:item[0].stable_key)),"forecast":tuple(sorted(forecast_event_keys)),"scenario":tuple(sorted(scenario_ids)),"strategy":tuple(sorted(strategy_bundle_ids)),"risk":tuple(sorted(risk_bundle_ids)),"portfolio":portfolio_bundle_id,"learning":tuple(sorted(learning_snapshot_ids)),"prompt_input_version":prompt_input_version}
        return ResearchContext(stable_hash(identity),scope,market,mode,cutoff_at,manifest,tuple(instrument_roles),tuple(forecast_event_keys),tuple(scenario_ids),tuple(strategy_bundle_ids),tuple(risk_bundle_ids),portfolio_bundle_id,tuple(learning_snapshot_ids),prompt_input_version,generated_at)
