from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ortools.sat.python import cp_model


RATE_SCALE = 1000


@dataclass(frozen=True, slots=True)
class SoftObjectiveWeights:
    consecutive_days: int = 5
    friday_rule: int = 15
    preferred_assignment: int = 20
    annual_hours_balance: int = 4
    accumulated_zone_equity: int = 3
    accumulated_turn_equity: int = 3
    zone_changes_tiebreak: int = 1
    turn_changes_tiebreak: int = 1


@dataclass(slots=True)
class CoreModel:
    model: cp_model.CpModel
    candidate_pairs: tuple[tuple[str, str], ...]
    assignment_vars: dict[tuple[str, str], cp_model.IntVar]
    coverage_vars: dict[str, cp_model.IntVar]
    vars_by_worker_day: dict[tuple[str, date], list[cp_model.IntVar]]
    need_ids_by_worker: dict[str, list[str]]
    incompatibility_constraints: int


@dataclass(slots=True)
class SoftComponents:
    plan_alterations: cp_model.IntVar
    preferred_assignment_violations: cp_model.IntVar
    consecutive_excess: cp_model.IntVar
    friday_violation: cp_model.IntVar
    zone_changes: cp_model.IntVar
    turn_changes: cp_model.IntVar
    annual_hours_range_minutes: cp_model.IntVar
    annual_hours_range_scaled: cp_model.IntVar
    annual_hours_equity_penalty: cp_model.IntVar
    accumulated_zone_rate_range: cp_model.IntVar
    accumulated_turn_rate_range: cp_model.IntVar
    accumulated_zone_equity_penalty: cp_model.IntVar
    accumulated_turn_equity_penalty: cp_model.IntVar
    worst_change_equity_gap: cp_model.IntVar
    operational_penalty: cp_model.IntVar
    annual_fairness_objective: cp_model.IntVar
    change_fairness_objective: cp_model.IntVar
    change_tiebreak_penalty: cp_model.IntVar
    normalized_total_changes: cp_model.IntVar
    opportunistic_equity_objective: cp_model.IntVar
