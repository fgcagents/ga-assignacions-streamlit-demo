from __future__ import annotations

from dataclasses import replace
from datetime import date
from time import monotonic

from ortools.sat.python import cp_model

from .configuration import PriorityPolicy
from .constraints import CoreModel, SoftObjectiveWeights
from .constraints.soft.operational import is_zone_change
from .domain import Need, OptimizationPhase, SolveResult, Worker
from .model import PlannerCore, SolverConfig


class PriorityPlanner(PlannerCore):
    """Construeix el pla per prioritats simples i acumulatives."""

    def __init__(self, problem, *, penalize_zone_streak: bool = True):
        super().__init__(problem)
        self.penalize_zone_streak = penalize_zone_streak

    def total_time_limit(self, config: SolverConfig) -> float:
        if config.max_time_seconds is None:
            return 120.0
        return min(config.max_time_seconds, 300.0)

    def solve(self, config: SolverConfig | None = None) -> SolveResult:
        config = config or SolverConfig()
        self._validate_config(config)
        started_at = monotonic()
        deadline = started_at + self.total_time_limit(config)
        core = self._build_core_model()
        model = core.model
        phases: list[OptimizationPhase] = []

        coverage = sum(core.coverage_vars.values())
        final_solver, coverage_phase = self._optimize(
            model,
            core,
            config,
            phases,
            coverage,
            "cobertura",
            maximize=True,
            deadline=deadline,
        )
        if coverage_phase.status not in {"FEASIBLE", "OPTIMAL"}:
            return self._empty_result(core, phases)
        if coverage_phase.status != "OPTIMAL":
            return self._finish_result(
                core,
                phases,
                final_solver,
                coverage_phase,
                started_at,
                complete=False,
            )
        model.add(coverage == round(coverage_phase.objective_value or 0))

        covered_minutes = sum(
            self.needs[need_id].duration_minutes * variable
            for need_id, variable in core.coverage_vars.items()
        )
        final_solver, phase = self._optimize(
            model,
            core,
            config,
            phases,
            covered_minutes,
            "hores_cobertes",
            maximize=True,
            deadline=deadline,
            hint_solver=final_solver,
        )
        if phase.status != "OPTIMAL":
            return self._finish_result(
                core,
                phases,
                final_solver,
                coverage_phase,
                started_at,
                complete=False,
            )
        model.add(covered_minutes == round(phase.objective_value or 0))

        days = sorted({need.date for need in self.problem.needs})
        plan_alterations = self._plan_alterations(core)
        remaining_priority_phases = 2 * len(days) + int(
            plan_alterations is not None
        )
        all_priority_phases_optimal = True
        if plan_alterations is not None:
            final_solver, phase = self._optimize(
                model,
                core,
                config,
                phases,
                plan_alterations,
                "estabilitat_pla",
                maximize=False,
                deadline=deadline,
                hint_solver=final_solver,
                time_limit_seconds=self._fair_phase_time_limit(
                    deadline,
                    remaining_priority_phases,
                ),
            )
            all_priority_phases_optimal &= phase.status == "OPTIMAL"
            model.add(
                plan_alterations
                == round(final_solver.value(plan_alterations))
            )
            remaining_priority_phases -= 1

        accumulated = self._initial_accumulated_values()
        for day in days:
            objectives = self._daily_objectives(
                core,
                day,
                accumulated,
                config.soft_weights,
                config.priority_policy,
            )
            for name, expression, maximize in objectives:
                final_solver, phase = self._optimize(
                    model,
                    core,
                    config,
                    phases,
                    expression,
                    f"{name}__{day.isoformat()}",
                    maximize=maximize,
                    deadline=deadline,
                    hint_solver=final_solver,
                    time_limit_seconds=self._fair_phase_time_limit(
                        deadline,
                        remaining_priority_phases,
                    ),
                )
                all_priority_phases_optimal &= phase.status == "OPTIMAL"
                model.add(expression == round(final_solver.value(expression)))
                remaining_priority_phases -= 1

            self._freeze_day(model, core, final_solver, day)
            self._update_accumulated(core, final_solver, day, accumulated)

        return self._finish_result(
            core,
            phases,
            final_solver,
            coverage_phase,
            started_at,
            complete=all_priority_phases_optimal,
        )

    @staticmethod
    def _fair_phase_time_limit(
        deadline: float,
        remaining_phase_count: int,
    ) -> float:
        """Reparteix temps segons la mida del model encara no congelada."""

        remaining_seconds = max(0.001, deadline - monotonic())
        remaining_days = max(1, (remaining_phase_count + 1) // 2)
        remaining_weight = (
            remaining_days * (remaining_days + 1)
            if remaining_phase_count % 2 == 0
            else remaining_days**2
        )
        return max(
            0.001,
            0.95
            * remaining_seconds
            * remaining_days
            / remaining_weight,
        )

    def _optimize(
        self,
        model: cp_model.CpModel,
        core: CoreModel,
        config: SolverConfig,
        phases: list[OptimizationPhase],
        expression,
        name: str,
        *,
        maximize: bool,
        deadline: float,
        hint_solver: cp_model.CpSolver | None = None,
        time_limit_seconds: float | None = None,
    ) -> tuple[cp_model.CpSolver, OptimizationPhase]:
        if hint_solver is not None:
            self._add_assignment_hints(model, core, hint_solver)
        model.clear_objective()
        if maximize:
            model.maximize(expression)
        else:
            model.minimize(expression)
        solver, phase = self._solve_phase(
            model,
            config,
            name,
            maximize=maximize,
            time_limit_seconds=min(
                max(0.001, deadline - monotonic()),
                (
                    time_limit_seconds
                    if time_limit_seconds is not None
                    else float("inf")
                ),
            ),
        )
        phases.append(phase)
        if (
            phase.status not in {"FEASIBLE", "OPTIMAL"}
            and hint_solver is not None
        ):
            return hint_solver, phase
        if phase.status == "FEASIBLE" and hint_solver is not None:
            candidate_value = solver.value(expression)
            hint_value = hint_solver.value(expression)
            hint_is_better = (
                hint_value > candidate_value
                if maximize
                else hint_value < candidate_value
            )
            if hint_is_better:
                phase = replace(
                    phase,
                    objective_value=float(hint_value),
                    relative_gap=self._relative_gap(
                        float(hint_value),
                        phase.best_objective_bound,
                        maximize=maximize,
                    ),
                )
                phases[-1] = phase
                return hint_solver, phase
        return solver, phase

    def _plan_alterations(self, core: CoreModel):
        stable_references = tuple(
            assignment
            for assignment in self.problem.reference_assignments
            if assignment.need_id not in self.problem.affected_need_ids
        )
        if not stable_references:
            return None
        preserved = [
            core.assignment_vars[(assignment.worker_id, assignment.need_id)]
            for assignment in stable_references
            if (assignment.worker_id, assignment.need_id)
            in core.assignment_vars
        ]
        alterations = core.model.new_int_var(
            0,
            len(stable_references),
            "priority_plan_alterations",
        )
        core.model.add(alterations == len(stable_references) - sum(preserved))
        return alterations

    def _initial_accumulated_values(self) -> dict[str, dict[str, int]]:
        zone_change_streaks = {worker.id: 0 for worker in self.problem.workers}
        for assignment in sorted(
            self.problem.history,
            key=lambda item: (item.start, item.end, item.worker_id),
        ):
            zone_change_streaks[assignment.worker_id] = (
                zone_change_streaks[assignment.worker_id] + 1
                if assignment.zone_change
                else 0
            )
        return {
            worker.id: {
                "zone": max(
                    0,
                    worker.historical_assignments
                    - worker.historical_zone_changes,
                ),
                "turn": max(
                    0,
                    worker.historical_assignments
                    - worker.historical_turn_changes,
                ),
                "minutes": worker.annual_minutes,
                "zone_change_streak": zone_change_streaks[worker.id],
            }
            for worker in self.problem.workers
            if worker.group == "T"
        }

    def _daily_objectives(
        self,
        core: CoreModel,
        day: date,
        accumulated: dict[str, dict[str, int]],
        weights: SoftObjectiveWeights,
        policy: PriorityPolicy = PriorityPolicy(),
    ) -> tuple[tuple[str, object, bool], ...]:
        pairs = tuple(
            (worker_id, need_id, variable)
            for (worker_id, need_id), variable in core.assignment_vars.items()
            if self.needs[need_id].date == day
        )
        preference_kinds = {
            (worker_id, need_id): self._preference_kind(
                self.workers[worker_id], self.needs[need_id]
            )
            for worker_id, need_id, _variable in pairs
        }
        current_preference_score = sum(
            self._preference_score(
                preference_kinds[(worker_id, need_id)],
                weights,
            )
            * variable
            for worker_id, need_id, variable in pairs
        )
        preference_coefficients = {
            (worker_id, need_id): self._accumulated_preference_score(
                accumulated[worker_id],
                preference_kinds[(worker_id, need_id)],
                weights,
            )
            for worker_id, need_id, _variable in pairs
        }
        accumulated_preference_cost = sum(
            preference_coefficients[(worker_id, need_id)] * variable
            for worker_id, need_id, variable in pairs
        )
        accumulated_hours_cost = sum(
            accumulated[worker_id]["minutes"]
            * self.needs[need_id].duration_minutes
            * variable
            for worker_id, need_id, variable in pairs
        )
        zone_streak_coefficients = {
            (worker_id, need_id): max(
                0,
                accumulated[worker_id]["zone_change_streak"]
                + 1
                - policy.zone_streak_threshold,
            )
            if self.penalize_zone_streak and policy.zone_streak_enabled and is_zone_change(
                self.workers[worker_id],
                self.needs[need_id],
            )
            else 0
            for worker_id, need_id, _variable in pairs
        }
        zone_streak_penalty = sum(
            zone_streak_coefficients[(worker_id, need_id)] * variable
            for worker_id, need_id, variable in pairs
        )
        stable_pair_rank = {
            pair: index + 1
            for index, pair in enumerate(
                sorted((worker_id, need_id) for worker_id, need_id, _ in pairs)
            )
        }
        stable_tiebreak = sum(
            stable_pair_rank[(worker_id, need_id)] * variable
            for worker_id, need_id, variable in pairs
        )

        accumulated_preference_bound = sum(preference_coefficients.values())
        current_preference_bound = sum(
            self._preference_score(
                preference_kinds[(worker_id, need_id)],
                weights,
            )
            for worker_id, need_id, _variable in pairs
        )
        stable_tiebreak_bound = sum(stable_pair_rank.values())
        preference_priority = (
            current_preference_score * (accumulated_preference_bound + 1)
            - accumulated_preference_cost
        ) * (stable_tiebreak_bound + 1) - stable_tiebreak
        preference_priority_range = (
            current_preference_bound * (accumulated_preference_bound + 1)
            + accumulated_preference_bound
        ) * (stable_tiebreak_bound + 1) + stable_tiebreak_bound
        zone_priority = (
            preference_priority
            - zone_streak_penalty * (preference_priority_range + 1)
        )
        return (
            ("prioritat_hores", accumulated_hours_cost, False),
            ("prioritat_preferencies", zone_priority, True),
        )

    @staticmethod
    def _preference_kind(worker: Worker, need: Need) -> str:
        zone_match = bool(
            worker.home_zone
            and need.zone
            and worker.home_zone == need.zone
        )
        turn_match = bool(
            worker.turn_options
            and need.turn_options
            and not worker.turn_options.isdisjoint(need.turn_options)
        )
        if zone_match and turn_match:
            return "both"
        if zone_match:
            return "zone"
        if turn_match:
            return "turn"
        return "none"

    @staticmethod
    def _preference_score(
        preference_kind: str,
        weights: SoftObjectiveWeights,
    ) -> int:
        score = 0
        if preference_kind in {"both", "turn"}:
            score += weights.accumulated_turn_equity
        if preference_kind in {"both", "zone"}:
            score += weights.accumulated_zone_equity
        return score

    @staticmethod
    def _accumulated_preference_score(
        values: dict[str, int],
        preference_kind: str,
        weights: SoftObjectiveWeights,
    ) -> int:
        score = 0
        if preference_kind in {"both", "turn"}:
            score += weights.accumulated_turn_equity * values["turn"]
        if preference_kind in {"both", "zone"}:
            score += weights.accumulated_zone_equity * values["zone"]
        return score

    def _freeze_day(
        self,
        model: cp_model.CpModel,
        core: CoreModel,
        solver: cp_model.CpSolver,
        day: date,
    ) -> None:
        for (_worker_id, need_id), variable in core.assignment_vars.items():
            if self.needs[need_id].date == day:
                model.add(variable == solver.value(variable))

    def _update_accumulated(
        self,
        core: CoreModel,
        solver: cp_model.CpSolver,
        day: date,
        accumulated: dict[str, dict[str, int]],
    ) -> None:
        for (worker_id, need_id), variable in core.assignment_vars.items():
            need = self.needs[need_id]
            if need.date != day or not solver.value(variable):
                continue
            values = accumulated[worker_id]
            worker = self.workers[worker_id]
            kind = self._preference_kind(worker, need)
            if kind == "both":
                values["zone"] += 1
                values["turn"] += 1
            elif kind in {"zone", "turn"}:
                values[kind] += 1
            if is_zone_change(worker, need):
                values["zone_change_streak"] += 1
            elif worker.home_zone and need.zone:
                values["zone_change_streak"] = 0
            values["minutes"] += need.duration_minutes
