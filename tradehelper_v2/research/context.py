"""从调用方提供的冻结事实构建最小研究上下文，不联网、不读取数据库。"""
from __future__ import annotations

from tradehelper_v2.contracts import ContractViolation, ResearchContext, ResearchFact, ResearchFactManifest, ResearchScope, stable_hash


class ResearchContextBuilder:
    @staticmethod
    def _fact(instrument,key,value,status,available_at,source_ref,source_payload_hash,unit=None,value_type=None):
        value_type=value_type or ("boolean" if isinstance(value,bool) else "number" if isinstance(value,(int,float)) else "text")
        identity={"instrument":instrument,"key":key,"value":value,"status":status,"available_at":available_at,"source_refs":(source_ref,),"source_payload_hash":source_payload_hash}
        return ResearchFact(stable_hash(identity),instrument,key,value,value_type,unit,status,available_at,(source_ref,),source_payload_hash)

    def project_upstream_facts(self, *, feature_snapshots=(), forecasts=(), scenarios=(), strategy_bundles=(), risk_bundles=(), portfolio_bundles=(), learning_snapshots=()):
        """只从冻结 V2 合同字段投影事实，生产调用方不自行拼 ResearchFact。"""
        facts=[]
        for snapshot in feature_snapshots:
            source=snapshot.feature_hash
            for item in snapshot.values:
                facts.append(self._fact(snapshot.instrument,f"feature.{item.name}",item.value,item.status.value,item.available_at,source,source,item.unit))
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
        for item in risk_bundles:
            hashed=stable_hash(item)
            facts.append(self._fact(item.instrument,f"risk.{item.risk_bundle_id}.decision_count",len(item.decisions),"available",item.generated_at,item.risk_bundle_id,hashed))
            facts.append(self._fact(item.instrument,f"risk.{item.risk_bundle_id}.levels",",".join(sorted(decision.level.value for decision in item.decisions)),"available",item.generated_at,item.risk_bundle_id,hashed))
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
