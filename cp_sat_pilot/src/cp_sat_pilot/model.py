from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from statistics import mean, median
from time import monotonic
from typing import Iterable

from ortools.sat.python import cp_model

from .configuration import PriorityPolicy
from .constraints import (
    CoreModel,
    HardConstraintSet,
    SoftObjectiveWeights,
)
from .constraints.soft.operational import (
    is_turn_change,
    is_zone_change,
)
from .domain import (
    Assignment,
    EquityWorkerDiagnostic,
    Need,
    OptimizationPhase,
    PlanningProblem,
    SocialDiagnosticSummary,
    SolveResult,
    Worker,
)


STATUS_NAMES = {
    cp_model.UNKNOWN: "UNKNOWN",
    cp_model.MODEL_INVALID: "MODEL_INVALID",
    cp_model.FEASIBLE: "FEASIBLE",
    cp_model.INFEASIBLE: "INFEASIBLE",
    cp_model.OPTIMAL: "OPTIMAL",
}
INFORMATIONAL_EQUITY_GAP_PERMILLE = 100


def _comparison_profile(worker: Worker) -> str:
    skills = "+".join(sorted(worker.skills)) or "-"
    turns = "+".join(sorted(worker.turn_options)) or "-"
    return f"habilitacions={skills}|zona={worker.home_zone or '-'}|torn={turns}"


def _percentile(values: list[int], proportion: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = proportion * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def _gini(values: list[int]) -> float:
    if not values or sum(values) == 0:
        return 0.0
    absolute_differences = sum(
        abs(first - second) for first in values for second in values
    )
    return absolute_differences / (2 * len(values) * sum(values))


@dataclass(frozen=True, slots=True)
class SolverConfig:
    max_time_seconds: float | None = None
    # Temps reservat dins del límit global per a la fase final d'equitat.
    equity_time_seconds: float | None = None
    num_workers: int = 8
    random_seed: int = 0
    log_search_progress: bool = False
    soft_weights: SoftObjectiveWeights = SoftObjectiveWeights()
    priority_policy: PriorityPolicy = PriorityPolicy()


class PlannerCore:
    """Candidats, validació i diagnòstics compartits; no resol planificacions."""

    def __init__(self, problem: PlanningProblem):
        self.problem = problem
        self.workers = {worker.id: worker for worker in problem.workers}
        self.needs = {need.id: need for need in problem.needs}
        self.hard_constraints = HardConstraintSet(problem)
        self.history_by_worker = self.hard_constraints.history_by_worker

    def is_static_candidate(self, worker: Worker, need: Need) -> bool:
        return self.hard_constraints.is_static_candidate(worker, need)

    def candidate_pairs(self) -> tuple[tuple[str, str], ...]:
        return self.hard_constraints.candidate_pairs()

    @staticmethod
    def is_zone_change(worker: Worker, need: Need) -> bool:
        return is_zone_change(worker, need)

    @staticmethod
    def is_turn_change(worker: Worker, need: Need) -> bool:
        return is_turn_change(worker, need)


    def _finish_result(
        self,
        core: CoreModel,
        phases: list[OptimizationPhase],
        solver: cp_model.CpSolver,
        coverage_phase: OptimizationPhase,
        started_at: float,
        *,
        complete: bool,
    ) -> SolveResult:
        assignments = self._extract_assignments(
            solver, core.assignment_vars
        )
        errors = tuple(self.validate(assignments))
        diagnostics, social_summary = self._build_equity_diagnostics(
            assignments
        )
        return SolveResult(
            status=(
                "OPTIMAL"
                if complete
                and all(phase.status == "OPTIMAL" for phase in phases)
                else "FEASIBLE"
            ),
            assignments=tuple(assignments),
            covered_needs=len(
                {assignment.need_id for assignment in assignments}
            ),
            total_needs=len(self.problem.needs),
            objective_value=coverage_phase.objective_value,
            best_objective_bound=coverage_phase.best_objective_bound,
            relative_gap=coverage_phase.relative_gap,
            wall_time_seconds=monotonic() - started_at,
            conflicts=sum(phase.conflicts for phase in phases),
            branches=sum(phase.branches for phase in phases),
            candidate_variables=len(core.candidate_pairs),
            incompatibility_constraints=core.incompatibility_constraints,
            validation_errors=errors,
            optimization_phases=tuple(phases),
            equity_diagnostics=diagnostics,
            social_diagnostic_summary=social_summary,
        )


    @staticmethod
    def _validate_config(config: SolverConfig) -> None:
        if config.max_time_seconds is not None and config.max_time_seconds <= 0:
            raise ValueError("El límit de temps ha de ser positiu")
        if (
            config.equity_time_seconds is not None
            and config.equity_time_seconds <= 0
        ):
            raise ValueError(
                "El límit de la fase d'equitat ha de ser positiu"
            )
        if config.num_workers <= 0:
            raise ValueError(
                "El nombre de workers del solver ha de ser positiu"
            )
        weights = config.soft_weights
        if any(
            value < 0
            for value in (
                weights.consecutive_days,
                weights.friday_rule,
                weights.preferred_assignment,
                weights.annual_hours_balance,
                weights.accumulated_zone_equity,
                weights.accumulated_turn_equity,
                weights.zone_changes_tiebreak,
                weights.turn_changes_tiebreak,
            )
        ):
            raise ValueError(
                "Els pesos dels objectius tous no poden ser negatius"
            )

    def _build_core_model(self) -> CoreModel:
        core = self.hard_constraints.build_core_model()
        references = {a.need_id: a.worker_id for a in self.problem.reference_assignments}
        for (wid, nid), variable in core.assignment_vars.items():
            if nid in references:
                core.model.add_hint(variable, int(references[nid] == wid))
        return core

    @staticmethod
    def _add_assignment_hints(
        model: cp_model.CpModel,
        core: CoreModel,
        solver: cp_model.CpSolver,
    ) -> None:
        model.clear_hints()
        for variable in core.assignment_vars.values():
            model.add_hint(variable, solver.value(variable))

    @staticmethod
    def _configured_solver(
        config: SolverConfig,
        *,
        time_limit_seconds: float | None = None,
    ) -> cp_model.CpSolver:
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = (
            time_limit_seconds
            if time_limit_seconds is not None
            else config.max_time_seconds or 60.0
        )
        solver.parameters.num_workers = config.num_workers
        solver.parameters.random_seed = config.random_seed
        solver.parameters.log_search_progress = (
            config.log_search_progress
        )
        return solver

    def _solve_phase(
        self,
        model: cp_model.CpModel,
        config: SolverConfig,
        name: str,
        *,
        maximize: bool,
        time_limit_seconds: float | None = None,
    ) -> tuple[cp_model.CpSolver, OptimizationPhase]:
        solver = self._configured_solver(
            config,
            time_limit_seconds=time_limit_seconds,
        )
        status_code = solver.solve(model)
        status = STATUS_NAMES.get(
            status_code, f"STATUS_{status_code}"
        )
        has_solution = status in {"FEASIBLE", "OPTIMAL"}
        objective = (
            float(solver.objective_value) if has_solution else None
        )
        bound = (
            float(solver.best_objective_bound)
            if has_solution
            else None
        )
        gap = self._relative_gap(
            objective, bound, maximize=maximize
        )
        return solver, OptimizationPhase(
            name=name,
            status=status,
            objective_value=objective,
            best_objective_bound=bound,
            relative_gap=gap,
            wall_time_seconds=float(solver.wall_time),
            conflicts=int(solver.num_conflicts),
            branches=int(solver.num_branches),
        )

    def _extract_assignments(
        self,
        solver: cp_model.CpSolver,
        assignment_vars: dict[
            tuple[str, str], cp_model.IntVar
        ],
    ) -> list[Assignment]:
        assignments: list[Assignment] = []
        for (worker_id, need_id), variable in assignment_vars.items():
            if solver.value(variable):
                need = self.needs[need_id]
                assignments.append(
                    Assignment(
                        worker_id=worker_id,
                        need_id=need_id,
                        service_id=need.service_id,
                        date=need.date,
                        start=need.start,
                        end=need.end,
                        duration_minutes=need.duration_minutes,
                    )
                )
        assignments.sort(
            key=lambda item: (
                item.date,
                item.start,
                item.service_id,
            )
        )
        return assignments


    def _build_equity_diagnostics(
        self,
        assignments: Iterable[Assignment],
    ) -> tuple[
        tuple[EquityWorkerDiagnostic, ...], SocialDiagnosticSummary
    ]:
        """Calcula informació posterior; no altera ni bloqueja el solver."""

        assigned_minutes: dict[str, int] = {}
        assigned_counts: dict[str, int] = {}
        zone_exception_counts: dict[str, int] = {}
        turn_exception_counts: dict[str, int] = {}
        double_exception_counts: dict[str, int] = {}
        preference_exception_counts: dict[str, int] = {}
        night_counts: dict[str, int] = {}
        for assignment in assignments:
            worker = self.workers[assignment.worker_id]
            need = self.needs[assignment.need_id]
            assigned_minutes[assignment.worker_id] = (
                assigned_minutes.get(assignment.worker_id, 0)
                + assignment.duration_minutes
            )
            assigned_counts[assignment.worker_id] = (
                assigned_counts.get(assignment.worker_id, 0) + 1
            )
            zone_exception = is_zone_change(worker, need)
            turn_exception = is_turn_change(worker, need)
            if zone_exception:
                zone_exception_counts[worker.id] = (
                    zone_exception_counts.get(worker.id, 0) + 1
                )
            if turn_exception:
                turn_exception_counts[worker.id] = (
                    turn_exception_counts.get(worker.id, 0) + 1
                )
            if zone_exception and turn_exception:
                double_exception_counts[worker.id] = (
                    double_exception_counts.get(worker.id, 0) + 1
                )
            if zone_exception or turn_exception:
                preference_exception_counts[worker.id] = (
                    preference_exception_counts.get(worker.id, 0) + 1
                )
            if need.is_night:
                night_counts[worker.id] = night_counts.get(worker.id, 0) + 1

        workers = tuple(
            worker for worker in self.problem.workers if worker.group == "T"
        )
        profiles = {worker.id: _comparison_profile(worker) for worker in workers}
        profile_sizes = Counter(
            profiles[worker.id]
            for worker in workers
            if worker.compatible_opportunities > 0
        )
        comparable_rates = []
        for worker in workers:
            comparable = (
                worker.annual_equity_target_minutes > 0
                and worker.compatible_opportunities > 0
            )
            if comparable:
                annual_total = worker.annual_minutes + assigned_minutes.get(
                    worker.id, 0
                )
                comparable_rates.append(
                    annual_total
                    * 1000
                    // max(1, worker.annual_equity_target_minutes)
                )

        reference_rate = median(comparable_rates) if comparable_rates else 0
        diagnostics: list[EquityWorkerDiagnostic] = []
        for worker in workers:
            annual_total = worker.annual_minutes + assigned_minutes.get(
                worker.id, 0
            )
            completion_rate = (
                annual_total
                * 1000
                // max(1, worker.annual_equity_target_minutes)
            )
            comparable = (
                worker.annual_equity_target_minutes > 0
                and worker.compatible_opportunities > 0
            )
            codes = [
                "equitat_exclusiva_grup_T",
                "referencia_contractual_75",
            ]
            if worker.annual_absence_days:
                codes.append("objectiu_ajustat_per_baixa")
            if worker.compatible_opportunities == 0:
                codes.append("sense_oportunitats_compatibles")
            group_size = profile_sizes.get(profiles[worker.id], 0)
            if worker.compatible_opportunities <= 0:
                comparison_status = "sense oportunitats compatibles"
            elif group_size < 2:
                comparison_status = "sense perfil comparable"
                codes.append("sense_perfil_comparable")
            else:
                comparison_status = "comparable"
            peer_gap = round(completion_rate - reference_rate) if comparable else 0
            absolute_gap = abs(peer_gap)
            if not comparable:
                review_status = "no_comparable"
            elif absolute_gap <= INFORMATIONAL_EQUITY_GAP_PERMILLE:
                review_status = "dins_marge"
            else:
                review_status = "alerta_informativa"
            if comparable:
                if peer_gap < -INFORMATIONAL_EQUITY_GAP_PERMILLE:
                    codes.append("desviacio_negativa_residual")
                elif peer_gap > INFORMATIONAL_EQUITY_GAP_PERMILLE:
                    codes.append("desviacio_positiva_residual")
                else:
                    codes.append("dins_marge_informatiu")
            historical_nights = max(
                worker.historical_night_services,
                sum(
                    assignment.is_night
                    for assignment in self.history_by_worker.get(worker.id, ())
                ),
            )
            diagnostics.append(
                EquityWorkerDiagnostic(
                    worker_id=worker.id,
                    annual_minutes=annual_total,
                    adjusted_target_minutes=worker.annual_equity_target_minutes,
                    completion_rate_permille=completion_rate,
                    absence_days=worker.annual_absence_days,
                    availability_basis_days=worker.annual_equity_basis_days,
                    compatible_opportunities=worker.compatible_opportunities,
                    compatible_opportunity_minutes=(
                        worker.compatible_opportunity_minutes
                    ),
                    assigned_opportunities=(
                        worker.historical_assignments
                        + assigned_counts.get(worker.id, 0)
                    ),
                    comparable=comparable,
                    justification_codes=tuple(codes),
                    peer_gap_permille=peer_gap,
                    review_status=review_status,
                    base_target_minutes=(
                        worker.annual_base_target_minutes
                    ),
                    flexible_target_minutes=(
                        worker.annual_flexible_target_minutes
                    ),
                    reliever_uplift_minutes=(
                        worker.annual_reliever_uplift_minutes
                    ),
                    maximum_minutes=worker.max_annual_minutes,
                    current_services=assigned_counts.get(worker.id, 0),
                    historical_services=worker.historical_assignments,
                    accumulated_services=(
                        worker.historical_assignments
                        + assigned_counts.get(worker.id, 0)
                    ),
                    current_zone_exception_services=(
                        zone_exception_counts.get(worker.id, 0)
                    ),
                    historical_zone_exception_services=(
                        worker.historical_zone_changes
                    ),
                    accumulated_zone_exception_services=(
                        worker.historical_zone_changes
                        + zone_exception_counts.get(worker.id, 0)
                    ),
                    current_turn_exception_services=(
                        turn_exception_counts.get(worker.id, 0)
                    ),
                    historical_turn_exception_services=(
                        worker.historical_turn_changes
                    ),
                    accumulated_turn_exception_services=(
                        worker.historical_turn_changes
                        + turn_exception_counts.get(worker.id, 0)
                    ),
                    current_double_exception_services=(
                        double_exception_counts.get(worker.id, 0)
                    ),
                    historical_double_exception_services=sum(
                        assignment.zone_change and assignment.turn_change
                        for assignment in self.history_by_worker.get(
                            worker.id, ()
                        )
                    ),
                    accumulated_double_exception_services=(
                        double_exception_counts.get(worker.id, 0)
                        + sum(
                            assignment.zone_change and assignment.turn_change
                            for assignment in self.history_by_worker.get(
                                worker.id, ()
                            )
                        )
                    ),
                    current_preference_exception_services=(
                        preference_exception_counts.get(worker.id, 0)
                    ),
                    historical_preference_exception_services=(
                        worker.historical_preference_exceptions
                    ),
                    accumulated_preference_exception_services=(
                        worker.historical_preference_exceptions
                        + preference_exception_counts.get(worker.id, 0)
                    ),
                    current_night_services=night_counts.get(worker.id, 0),
                    historical_night_services=historical_nights,
                    accumulated_night_services=(
                        historical_nights + night_counts.get(worker.id, 0)
                    ),
                    can_work_nights=worker.can_work_nights,
                    comparison_profile=profiles[worker.id],
                    comparison_group_size=group_size,
                    comparison_status=comparison_status,
                )
            )
        rate_mean = mean(comparable_rates) if comparable_rates else 0.0
        p10 = _percentile(comparable_rates, 0.10)
        p90 = _percentile(comparable_rates, 0.90)
        summary = SocialDiagnosticSummary(
            worker_count=len(workers),
            comparable_worker_count=len(comparable_rates),
            comparison_profile_count=len(profile_sizes),
            unique_profile_worker_count=sum(
                size == 1 for size in profile_sizes.values()
            ),
            current_services=sum(assigned_counts.values()),
            current_zone_exception_services=sum(zone_exception_counts.values()),
            current_turn_exception_services=sum(turn_exception_counts.values()),
            current_double_exception_services=sum(double_exception_counts.values()),
            current_night_services=sum(night_counts.values()),
            night_capable_worker_count=sum(
                worker.can_work_nights for worker in workers
            ),
            completion_rate_mean_absolute_deviation_permille=round(
                mean(abs(value - rate_mean) for value in comparable_rates), 3
            )
            if comparable_rates
            else 0.0,
            completion_rate_gini=round(_gini(comparable_rates), 6),
            completion_rate_p10_permille=round(p10, 3),
            completion_rate_p90_permille=round(p90, 3),
            completion_rate_p90_p10_gap_permille=round(p90 - p10, 3),
            night_history_available=True,
            current_preference_exception_services=sum(
                preference_exception_counts.values()
            ),
        )
        return tuple(diagnostics), summary

    def _empty_result(
        self,
        core: CoreModel,
        phases: list[OptimizationPhase],
    ) -> SolveResult:
        phase = phases[0]
        return SolveResult(
            status=phase.status,
            assignments=(),
            covered_needs=0,
            total_needs=len(self.problem.needs),
            objective_value=phase.objective_value,
            best_objective_bound=phase.best_objective_bound,
            relative_gap=phase.relative_gap,
            wall_time_seconds=phase.wall_time_seconds,
            conflicts=phase.conflicts,
            branches=phase.branches,
            candidate_variables=len(core.candidate_pairs),
            incompatibility_constraints=(
                core.incompatibility_constraints
            ),
            optimization_phases=tuple(phases),
        )

    @staticmethod
    def _relative_gap(
        objective: float | None,
        bound: float | None,
        *,
        maximize: bool,
    ) -> float | None:
        if objective is None or bound is None:
            return None
        difference = (
            bound - objective if maximize else objective - bound
        )
        return max(0.0, difference) / max(
            1.0, abs(bound), abs(objective)
        )

    def validate(
        self, assignments: Iterable[Assignment]
    ) -> list[str]:
        return self.hard_constraints.validate(assignments)
