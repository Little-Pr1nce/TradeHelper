"""组合级现金、heat、仓位和相关性约束的纯函数分配。"""
from __future__ import annotations

from decimal import Decimal

from contracts import (
    AllocationStatus, CorrelationStatus, DecisionDisposition, ExecutionLevel,
    EvidenceStatus, PlanAction, PortfolioAllocation, PortfolioEvidenceGrade, PortfolioProfileDecision,
    PortfolioReservationGroup, PortfolioReservationSnapshot, RiskProfile,
    stable_hash,
)
from risk.sizing import cash_required, planned_loss, round_lot_down

from .ranking import rank_components, rank_entries, rank_holdings


_ZERO = Decimal("0")


class PortfolioAllocator:
    """逐候选 waterfall；卖出估算回笼只展示，绝不加入本轮可用现金。"""

    def _correlation_limit(self, *, candidate, batch, current_values, reserved_values, pair_map):
        """计算 35% 直接邻域占用和缺失证据降级标志。"""
        instrument = candidate.trade_plan.instrument
        neighborhood = current_values.get(instrument, _ZERO) + reserved_values.get(instrument, _ZERO)
        missing = False
        instruments=set(current_values)|set(reserved_values)
        for other in instruments:
            if other == instrument:
                continue
            value=current_values.get(other,_ZERO)+reserved_values.get(other,_ZERO)
            pair = pair_map.get(frozenset((instrument, other)))
            if pair is None or pair.status is not CorrelationStatus.COMPLETE:
                neighborhood += value
                missing = True
            elif pair.coefficient >= batch.portfolio_policy.high_correlation_threshold:
                neighborhood += value
        return neighborhood, missing

    def capacity(self, *, candidate, remaining_cash, remaining_heat, equity, invested, current_value, correlation_value, correlation_missing, portfolio_policy, risk_policy):
        """以完整 Decimal 费用公式回退 lot，不能把最低佣金线性化。"""
        decision, rules = candidate.execution_decision, candidate.market_rules
        entry, stop = decision.entry_price, decision.stop_price
        if entry is None or stop is None or entry <= stop:
            return _ZERO, ("PORTFOLIO_ZERO_CAPACITY",)
        approved_cap=round_lot_down(decision.approved_shares,rules.lot_size)
        cap=approved_cap
        reasons: list[str] = []
        if approved_cap!=decision.approved_shares: reasons.append("PORTFOLIO_LOT_ROUNDED")
        cash_unit=entry*(Decimal("1")+rules.commission_rate+rules.base_slippage_reserve)
        loss_unit=(entry-stop)+(entry+stop)*rules.base_slippage_reserve+entry*rules.commission_rate+stop*(rules.commission_rate+rules.sell_tax_rate)
        limits=(
            (round_lot_down(max(_ZERO,remaining_cash/cash_unit),rules.lot_size),"PORTFOLIO_CASH_LIMITED"),
            (round_lot_down(max(_ZERO,remaining_heat/loss_unit),rules.lot_size),"PORTFOLIO_HEAT_LIMITED"),
            (round_lot_down(max(_ZERO,(equity*risk_policy.single_position_hard_cap-current_value)/entry),rules.lot_size),"PORTFOLIO_SINGLE_POSITION_LIMITED"),
            (round_lot_down(max(_ZERO,(equity*risk_policy.total_stock_hard_cap-invested)/entry),rules.lot_size),"PORTFOLIO_TOTAL_EXPOSURE_LIMITED"),
            (round_lot_down(max(_ZERO,(equity*portfolio_policy.high_correlation_group_cap-correlation_value)/entry),rules.lot_size),"PORTFOLIO_HIGH_CORRELATION_LIMITED"),
        )
        for limit,reason in limits:
            if limit<cap: reasons.append(reason)
            cap=min(cap,limit)
        if cap <= 0:
            return _ZERO, tuple(sorted(set(reasons or ["PORTFOLIO_ZERO_CAPACITY"])))
        if correlation_missing:
            cap = round_lot_down(cap * portfolio_policy.unknown_correlation_multiplier, rules.lot_size)
            reasons.extend(("PORTFOLIO_CORRELATION_EVIDENCE_MISSING", "PORTFOLIO_CORRELATION_MULTIPLIER_APPLIED"))
        original = cap
        while cap > 0:
            cash = cash_required(cap, entry, rules)
            loss = planned_loss(cap, entry, stop, rules)
            failures: list[str] = []
            if cash > remaining_cash: failures.append("PORTFOLIO_CASH_LIMITED")
            if loss > remaining_heat: failures.append("PORTFOLIO_HEAT_LIMITED")
            if current_value + cap * entry > equity * risk_policy.single_position_hard_cap: failures.append("PORTFOLIO_SINGLE_POSITION_LIMITED")
            if invested + cap * entry > equity * risk_policy.total_stock_hard_cap: failures.append("PORTFOLIO_TOTAL_EXPOSURE_LIMITED")
            if not failures:
                if cap < original: reasons.append("PORTFOLIO_LOT_ROUNDED")
                return cap, tuple(sorted(set(reasons)))
            reasons.extend(failures)
            cap -= rules.lot_size
        return _ZERO, tuple(sorted(set(reasons or ["PORTFOLIO_ZERO_CAPACITY"])))

    @staticmethod
    def _allocation_id(batch,profile,candidate,status,final,rank,reasons,*,reserved_cash=_ZERO,reserved_loss=_ZERO,rank_components=(),estimated_pct=None,binding_constraints=()):
        decision = candidate.execution_decision
        reason_codes = tuple(sorted(set(reasons)))
        components=tuple((str(key),str(value)) for key,value in rank_components); constraints=tuple(sorted(set(binding_constraints)))
        identity={"batch_id":batch.batch_id,"profile":profile,"candidate_id":candidate.candidate_id,"instrument":candidate.trade_plan.instrument,"plan_id":candidate.trade_plan.plan_id,"decision_id":decision.decision_id,"action":candidate.trade_plan.action,"level":decision.level.value,"status":status,"rank":rank,"rank_components":components,"approved":decision.approved_shares,"final":final,"current_value":decision.current_position_value,"entry":decision.entry_price,"reserved_cash":reserved_cash,"reserved_loss":reserved_loss,"estimated_pct":estimated_pct,"constraints":constraints,"reasons":reason_codes}
        return stable_hash(identity)

    def _allocation(self, batch, profile, candidate, status, final, rank, reasons, generated_at, *, reserved_cash=_ZERO, reserved_loss=_ZERO, group_id=None, rank_components=(),estimated_pct=None,binding_constraints=()):
        decision=candidate.execution_decision; reason_codes=tuple(sorted(set(reasons))); constraints=tuple(sorted(set(binding_constraints)))
        return PortfolioAllocation(
            self._allocation_id(batch,profile,candidate,status,final,rank,reason_codes,reserved_cash=reserved_cash,reserved_loss=reserved_loss,rank_components=rank_components,estimated_pct=estimated_pct,binding_constraints=constraints), batch.batch_id, profile, candidate.candidate_id,
            candidate.trade_plan.instrument, candidate.trade_plan.plan_id, decision.decision_id,
            candidate.trade_plan.action, decision.level.value, status, rank, tuple(rank_components),
            decision.approved_shares, final, decision.current_position_value, decision.entry_price,
            reserved_cash, reserved_loss, estimated_pct, group_id,
            constraints, reason_codes, generated_at,
        )

    def allocate(self, batch, snapshot, generated_at):
        return tuple(self._profile(batch, snapshot, profile, generated_at) for profile in (RiskProfile.CONSERVATIVE, RiskProfile.AGGRESSIVE))

    def _profile(self, batch, snapshot, profile, generated_at):
        candidates = [item for item in batch.candidates if item.execution_decision.profile is profile]
        allocations: list[PortfolioAllocation] = []
        groups: list[PortfolioReservationGroup] = []
        protective_ids = {value for bundle in batch.risk_bundles for value in bundle.protective_decision_ids}
        positions = {item.instrument: item for item in batch.account_snapshot.positions}
        valuations = {item.instrument: item for item in batch.valuation.position_values}

        exit_specs: list[tuple[int, object, tuple[str, ...], Decimal]] = []
        holding_priority: list[str] = []
        exit_release = _ZERO
        holding_candidates = [
            item for item in candidates
            if item.role.value == "holding"
            and item.trade_plan.action in {PlanAction.SELL, PlanAction.REDUCE}
        ]
        for rank, candidate in enumerate(rank_holdings(holding_candidates, protective_ids), 1):
            decision, action = candidate.execution_decision, candidate.trade_plan.action
            executable_exit = (
                action in {PlanAction.SELL, PlanAction.REDUCE}
                and decision.level in {ExecutionLevel.A, ExecutionLevel.B}
                and decision.disposition in {DecisionDisposition.APPROVED_NOW, DecisionDisposition.CONDITIONALLY_APPROVED}
                and decision.approved_shares > 0
            )
            components = rank_components(candidate)
            if not executable_exit:
                allocations.append(self._allocation(
                    batch, profile, candidate, AllocationStatus.NO_ORDER, _ZERO, rank,
                    ("PORTFOLIO_NO_ORDER_UPSTREAM",), generated_at, rank_components=components,
                ))
                continue
            available = positions[candidate.trade_plan.instrument].shares
            final = min(decision.approved_shares, available)
            reasons = ["PORTFOLIO_EXIT_PROCEEDS_NOT_REUSED"]
            if decision.decision_id in protective_ids:
                reasons.append("PORTFOLIO_PROTECTIVE_EXIT_PRIORITY")
            exit_specs.append((rank, candidate, tuple(reasons), final))

        # 同一持仓的多个退出条件共享真实可卖数量；估算回笼也只按最大可执行量计一次。
        instruments = sorted({item.trade_plan.instrument for _, item, _, _ in exit_specs}, key=lambda item: item.stable_key)
        for instrument in instruments:
            specs = [item for item in exit_specs if item[1].trade_plan.instrument == instrument]
            valuation = valuations.get(instrument)
            maximum = min(positions[instrument].shares, max(item[3] for item in specs))
            if valuation is not None:
                exit_release += maximum * valuation.price
            if len(specs) == 1:
                rank, candidate, reasons, final = specs[0]
                status = (AllocationStatus.ALLOCATED_NOW if candidate.execution_decision.disposition is DecisionDisposition.APPROVED_NOW
                          else AllocationStatus.RESERVED_CONDITIONAL)
                allocation = self._allocation(
                    batch, profile, candidate, status, final, rank, reasons, generated_at,
                    rank_components=rank_components(candidate),
                )
                allocations.append(allocation)
                holding_priority.append(allocation.allocation_id)
                continue

            member_specs = []
            for rank, candidate, base_reasons, final in specs:
                reasons = tuple(sorted(set(base_reasons + (
                    "PORTFOLIO_EXIT_RESERVATION_SHARED", "PORTFOLIO_EXIT_STATE_RECHECK_REQUIRED",
                ))))
                member_id = self._allocation_id(
                    batch, profile, candidate, AllocationStatus.SHARED_EXIT_RESERVATION,
                    final, rank, reasons, rank_components=rank_components(candidate),
                )
                member_specs.append((rank, candidate, reasons, final, member_id))
            members = tuple(item[4] for item in member_specs)
            group_reasons = ("PORTFOLIO_EXIT_RESERVATION_SHARED", "PORTFOLIO_EXIT_STATE_RECHECK_REQUIRED")
            identity = {
                "batch_id": batch.batch_id, "profile": profile, "instrument": instrument,
                "side": "sell", "members": tuple(sorted(members)), "maximum": maximum,
                "policy": "first_fill_consumes_then_recheck_v1", "reasons": group_reasons,
            }
            group = PortfolioReservationGroup(
                stable_hash(identity), batch.batch_id, profile, instrument, "sell", members, maximum,
                "first_fill_consumes_then_recheck_v1", group_reasons, generated_at,
            )
            groups.append(group)
            for rank, candidate, reasons, final, _ in member_specs:
                allocation = self._allocation(
                    batch, profile, candidate, AllocationStatus.SHARED_EXIT_RESERVATION,
                    final, rank, reasons, generated_at, group_id=group.group_id,
                    rank_components=rank_components(candidate),
                )
                allocations.append(allocation)
                holding_priority.append(allocation.allocation_id)

        total_cap = batch.risk_policy.total_stock_hard_cap
        deployable = _ZERO
        if batch.valuation.status.value == "complete" and snapshot.equity > 0:
            deployable = min(snapshot.cash, max(snapshot.equity * total_cap - snapshot.invested_value, _ZERO))
        heat_cap = (batch.portfolio_policy.conservative_heat_cap
                    if profile is RiskProfile.CONSERVATIVE else batch.portfolio_policy.aggressive_heat_cap)
        remaining_cash = deployable
        current_loss = snapshot.planned_loss_amount
        remaining_heat = max(snapshot.equity * heat_cap - (current_loss or _ZERO), _ZERO)
        current_values = {item.instrument: item.market_value for item in batch.valuation.position_values}
        reserved_values: dict = {}
        pair_map = {frozenset((item.left, item.right)): item for item in batch.correlation_snapshot.pairs}
        entry_ids: list[str] = []
        selected: set = set()
        entry_gate_reason = None
        if batch.valuation.status.value != "complete": entry_gate_reason = "PORTFOLIO_INCOMPLETE_VALUATION"
        elif snapshot.equity <= 0: entry_gate_reason = "PORTFOLIO_EQUITY_ZERO"
        elif snapshot.heat_status.value == "breached": entry_gate_reason = "PORTFOLIO_STOP_ALREADY_BREACHED"
        elif snapshot.heat_status.value != "complete": entry_gate_reason = "PORTFOLIO_HOLDING_RISK_UNKNOWN"
        elif snapshot.invested_pct >= total_cap or remaining_cash <= 0: entry_gate_reason = "PORTFOLIO_ZERO_CAPACITY"
        elif current_loss is not None and current_loss / snapshot.equity >= heat_cap: entry_gate_reason = "PORTFOLIO_HEAT_EXHAUSTED"

        entry_candidates = [item for item in candidates if item.trade_plan.action in {PlanAction.BUY, PlanAction.ADD}]
        for rank, candidate in enumerate(rank_entries(entry_candidates), 1):
            decision, instrument = candidate.execution_decision, candidate.trade_plan.instrument
            components = rank_components(candidate)
            if instrument in selected:
                allocation = self._allocation(
                    batch, profile, candidate, AllocationStatus.MONITOR_ONLY, _ZERO, rank,
                    ("PORTFOLIO_DUPLICATE_ENTRY_SUPPRESSED",), generated_at,
                    rank_components=components,
                )
                allocations.append(allocation); entry_ids.append(allocation.allocation_id)
                continue
            selected.add(instrument)
            eligible = (
                decision.level in {ExecutionLevel.A, ExecutionLevel.B}
                and decision.disposition in {DecisionDisposition.APPROVED_NOW, DecisionDisposition.CONDITIONALLY_APPROVED}
            )
            if not eligible:
                allocation = self._allocation(
                    batch, profile, candidate, AllocationStatus.NO_ORDER, _ZERO, rank,
                    ("PORTFOLIO_NO_ORDER_UPSTREAM",), generated_at, rank_components=components,
                )
                allocations.append(allocation); entry_ids.append(allocation.allocation_id)
                continue
            evidence_status = candidate.plan_evidence.status if candidate.plan_evidence else decision.evidence_status
            gate_reason = entry_gate_reason
            if evidence_status in {EvidenceStatus.NEGATIVE, EvidenceStatus.CONFLICTING}:
                gate_reason = "PORTFOLIO_NOT_SELECTED"
            if gate_reason:
                allocation = self._allocation(
                    batch, profile, candidate, AllocationStatus.BLOCKED, _ZERO, rank,
                    (gate_reason,), generated_at, rank_components=components,
                    binding_constraints=(gate_reason,),
                )
                allocations.append(allocation); entry_ids.append(allocation.allocation_id)
                continue
            correlation_value, missing = self._correlation_limit(
                candidate=candidate, batch=batch, current_values=current_values,
                reserved_values=reserved_values, pair_map=pair_map,
            )
            shares, reasons = self.capacity(
                candidate=candidate, remaining_cash=remaining_cash, remaining_heat=remaining_heat,
                equity=snapshot.equity,
                invested=snapshot.invested_value + sum(reserved_values.values(), _ZERO),
                current_value=current_values.get(instrument, _ZERO), correlation_value=correlation_value,
                correlation_missing=missing, portfolio_policy=batch.portfolio_policy,
                risk_policy=batch.risk_policy,
            )
            if not shares:
                reason_codes = reasons or ("PORTFOLIO_ZERO_CAPACITY",)
                allocation = self._allocation(
                    batch, profile, candidate, AllocationStatus.BLOCKED, _ZERO, rank,
                    reason_codes, generated_at, rank_components=components,
                    binding_constraints=reason_codes,
                )
                allocations.append(allocation); entry_ids.append(allocation.allocation_id)
                continue
            cash = cash_required(shares, decision.entry_price, candidate.market_rules)
            loss = planned_loss(shares, decision.entry_price, decision.stop_price, candidate.market_rules)
            status = (AllocationStatus.ALLOCATED_NOW if decision.disposition is DecisionDisposition.APPROVED_NOW
                      else AllocationStatus.RESERVED_CONDITIONAL)
            reason_codes = tuple(sorted(set(reasons + (("PORTFOLIO_ALLOCATED",)
                if status is AllocationStatus.ALLOCATED_NOW else ("PORTFOLIO_CONDITIONAL_RESERVATION",)))))
            estimated_pct = (current_values.get(instrument, _ZERO) + shares * decision.entry_price) / snapshot.equity
            allocation = self._allocation(
                batch, profile, candidate, status, shares, rank, reason_codes, generated_at,
                reserved_cash=cash, reserved_loss=loss, rank_components=components,
                estimated_pct=estimated_pct, binding_constraints=reasons,
            )
            allocations.append(allocation); entry_ids.append(allocation.allocation_id)
            remaining_cash -= cash; remaining_heat -= loss
            reserved_values[instrument] = reserved_values.get(instrument, _ZERO) + shares * decision.entry_price

        covered = {item.decision_id for item in allocations}
        for candidate in candidates:
            if candidate.execution_decision.decision_id not in covered:
                allocations.append(self._allocation(
                    batch, profile, candidate, AllocationStatus.NO_ORDER, _ZERO, None,
                    ("PORTFOLIO_NO_ORDER_UPSTREAM",), generated_at,
                ))
        reserved_cash = sum((item.reserved_cash for item in allocations), _ZERO)
        reserved_loss = sum((item.reserved_incremental_loss for item in allocations), _ZERO)
        reserved_notional = sum(reserved_values.values(), _ZERO)
        projected_heat = None if current_loss is None or snapshot.equity <= 0 else (current_loss + reserved_loss) / snapshot.equity
        projected_invested = ((snapshot.invested_value + reserved_notional) / snapshot.equity
                              if snapshot.equity > 0 else _ZERO)
        profile_grade = snapshot.evidence_grade
        if any("PORTFOLIO_CORRELATION_EVIDENCE_MISSING" in item.reason_codes for item in allocations):
            profile_grade = PortfolioEvidenceGrade.LOW
        elif profile_grade is PortfolioEvidenceGrade.HIGH and any(
                item.status is AllocationStatus.RESERVED_CONDITIONAL for item in allocations):
            profile_grade = PortfolioEvidenceGrade.MEDIUM
        reservation = PortfolioReservationSnapshot(
            profile, snapshot.equity, snapshot.cash, deployable, reserved_cash,
            deployable - reserved_cash, reserved_notional, projected_invested,
            current_loss, reserved_loss, projected_heat, exit_release,
            profile_grade, ("PORTFOLIO_EXIT_PROCEEDS_NOT_REUSED",),
        )
        holding_priority = [item.allocation_id for item in sorted(
            (item for item in allocations if item.action in {PlanAction.SELL, PlanAction.REDUCE}
             and item.final_requested_shares > 0),
            key=lambda item: (item.rank if item.rank is not None else 10**9, item.allocation_id),
        )]
        blocked = tuple(sorted(item.allocation_id for item in allocations if item.status is AllocationStatus.BLOCKED))
        reason_codes = ("PORTFOLIO_PROFILE_SEPARATED",)
        identity = {
            "batch_id": batch.batch_id, "profile": profile,
            "allocation_ids": tuple(sorted(item.allocation_id for item in allocations)),
            "group_ids": tuple(sorted(item.group_id for item in groups)),
            "holding_priority": tuple(holding_priority), "entry_priority": tuple(entry_ids),
            "blocked": blocked, "risk_snapshot": snapshot.risk_snapshot_id,
            "reservation": reservation, "replacement_ids": (), "grade": profile_grade,
            "reasons": reason_codes,
        }
        return PortfolioProfileDecision(
            stable_hash(identity), batch.batch_id, profile, tuple(allocations), tuple(groups),
            tuple(holding_priority), tuple(entry_ids), blocked, snapshot, reservation, (),
            profile_grade, reason_codes, generated_at,
        )
