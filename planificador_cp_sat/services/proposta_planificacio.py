"""Generació i comparació d'una proposta diferencial en memòria."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from cp_sat_pilot import Assignment, CpSatPlanner, Need, SolveResult, SolverConfig
from cp_sat_pilot.functional_validation import analyze_functional_result
from cp_sat_pilot.multistart import (
    MultiStartSelection,
    MultiStartSelectionError,
    solve_adaptive_multi_start,
)

from planificador_cp_sat.services.preparacio_planificacio import (
    PreparedPlanningProblem,
)


class PlanningProposalGenerationError(ValueError):
    """Indica que el solver no ha produït una proposta utilitzable."""


class PlanningChangeKind(StrEnum):
    """Operació diferencial que es podrà persistir posteriorment."""

    REASSIGNMENT = "reassignacio"
    ADDITION = "alta"
    REMOVAL = "baixa"


@dataclass(frozen=True, slots=True)
class PlanningAssignmentChange:
    """Canvi únic amb l'estat anterior i el proposat d'una necessitat."""

    kind: PlanningChangeKind
    need: Need
    before: Assignment | None
    after: Assignment | None

    def __post_init__(self) -> None:
        expected_presence = {
            PlanningChangeKind.REASSIGNMENT: (True, True),
            PlanningChangeKind.ADDITION: (False, True),
            PlanningChangeKind.REMOVAL: (True, False),
        }
        if (self.before is not None, self.after is not None) != (
            expected_presence[self.kind]
        ):
            raise ValueError("L'estat anterior i posterior no concorda amb el canvi")
        for assignment in (self.before, self.after):
            if assignment is not None and assignment.need_id != self.need.id:
                raise ValueError("El canvi conté una assignació d'una altra necessitat")
        if (
            self.kind is PlanningChangeKind.REASSIGNMENT
            and self.before is not None
            and self.after is not None
            and self.before.worker_id == self.after.worker_id
        ):
            raise ValueError("Una reassignació ha de canviar de treballador")

    @property
    def need_id(self) -> str:
        return self.need.id


@dataclass(frozen=True, slots=True)
class UncoveredPlanningNeed:
    """Necessitat que la proposta deixa descoberta amb diagnòstic funcional."""

    need: Need
    reason: str
    had_reference_assignment: bool
    static_candidates: int | None = None
    compatible_candidates: int | None = None

    @property
    def need_id(self) -> str:
        return self.need.id


@dataclass(frozen=True, slots=True)
class PlanningProposal:
    """Resultat diferencial complet, immutable i encara no persistit."""

    prepared: PreparedPlanningProblem
    selection: MultiStartSelection
    unchanged_assignments: tuple[Assignment, ...]
    changes: tuple[PlanningAssignmentChange, ...]
    uncovered_needs: tuple[UncoveredPlanningNeed, ...]
    solver_config: SolverConfig | None = None
    requested_seeds: tuple[int, ...] = ()
    force_all_seeds: bool = False

    @property
    def result(self) -> SolveResult:
        return self.selection.selected_result

    @property
    def snapshot_fingerprint(self) -> str:
        return self.prepared.snapshot.fingerprint

    @property
    def covered_needs(self) -> int:
        return self.result.covered_needs

    @property
    def total_needs(self) -> int:
        return self.result.total_needs

    @property
    def coverage_percent(self) -> float:
        if not self.total_needs:
            return 100.0
        return round(100 * self.covered_needs / self.total_needs, 2)

    @property
    def persistent_change_count(self) -> int:
        return len(self.changes)

    @property
    def reassignments(self) -> tuple[PlanningAssignmentChange, ...]:
        return tuple(
            item
            for item in self.changes
            if item.kind is PlanningChangeKind.REASSIGNMENT
        )

    @property
    def additions(self) -> tuple[PlanningAssignmentChange, ...]:
        return tuple(
            item
            for item in self.changes
            if item.kind is PlanningChangeKind.ADDITION
        )

    @property
    def removals(self) -> tuple[PlanningAssignmentChange, ...]:
        return tuple(
            item
            for item in self.changes
            if item.kind is PlanningChangeKind.REMOVAL
        )


def _indexed_assignments(
    assignments: Iterable[Assignment],
    *,
    label: str,
) -> dict[str, Assignment]:
    indexed: dict[str, Assignment] = {}
    for assignment in assignments:
        if assignment.need_id in indexed:
            raise PlanningProposalGenerationError(
                f"{label} conté més d'una assignació per a "
                f"{assignment.need_id}"
            )
        indexed[assignment.need_id] = assignment
    return indexed


def _uncovered_diagnostics(
    prepared: PreparedPlanningProblem,
    result: SolveResult,
    uncovered_need_ids: set[str],
) -> dict[str, dict]:
    if not uncovered_need_ids:
        return {}
    analysis = analyze_functional_result(prepared.problem, result)
    return {
        item["necessitat_id"]: item
        for item in analysis["diagnostic_descobertes"]
        if item["necessitat_id"] in uncovered_need_ids
    }


def planning_proposal_from_result(
    prepared: PreparedPlanningProblem,
    selection: MultiStartSelection,
    *,
    solver_config: SolverConfig | None = None,
    requested_seeds: tuple[int, ...] = (),
    force_all_seeds: bool = False,
) -> PlanningProposal:
    """Compara una solució vàlida amb la fotografia que l'ha originada."""
    if not isinstance(prepared, PreparedPlanningProblem):
        raise TypeError("prepared ha de ser PreparedPlanningProblem")
    if not isinstance(selection, MultiStartSelection):
        raise TypeError("selection ha de ser MultiStartSelection")
    result = selection.selected_result
    if not result.feasible:
        details = "; ".join(result.validation_errors[:5])
        suffix = f": {details}" if details else ""
        raise PlanningProposalGenerationError(
            "CP-SAT no ha produït una proposta factible i validada" + suffix
        )

    needs_by_id = {need.id: need for need in prepared.problem.needs}
    references = _indexed_assignments(
        prepared.problem.reference_assignments,
        label="El pla de referència",
    )
    proposed = _indexed_assignments(
        result.assignments,
        label="La solució",
    )
    unknown = set(proposed) - set(needs_by_id)
    if unknown:
        raise PlanningProposalGenerationError(
            "La solució conté necessitats desconegudes: "
            + ", ".join(sorted(unknown))
        )
    if result.total_needs != len(needs_by_id):
        raise PlanningProposalGenerationError(
            "El total de necessitats de la solució no concorda amb el problema"
        )
    if result.covered_needs != len(proposed):
        raise PlanningProposalGenerationError(
            "La cobertura declarada no concorda amb les assignacions proposades"
        )

    unchanged: list[Assignment] = []
    changes: list[PlanningAssignmentChange] = []
    uncovered_ids: set[str] = set()
    for need in sorted(
        prepared.problem.needs,
        key=lambda item: (item.date, item.service_id, item.id),
    ):
        before = references.get(need.id)
        after = proposed.get(need.id)
        if before is not None and after is not None:
            if before.worker_id == after.worker_id:
                unchanged.append(after)
            else:
                changes.append(
                    PlanningAssignmentChange(
                        kind=PlanningChangeKind.REASSIGNMENT,
                        need=need,
                        before=before,
                        after=after,
                    )
                )
        elif after is not None:
            changes.append(
                PlanningAssignmentChange(
                    kind=PlanningChangeKind.ADDITION,
                    need=need,
                    before=None,
                    after=after,
                )
            )
        else:
            uncovered_ids.add(need.id)
            if before is not None:
                changes.append(
                    PlanningAssignmentChange(
                        kind=PlanningChangeKind.REMOVAL,
                        need=need,
                        before=before,
                        after=None,
                    )
                )

    diagnostics = _uncovered_diagnostics(prepared, result, uncovered_ids)
    uncovered = tuple(
        UncoveredPlanningNeed(
            need=needs_by_id[need_id],
            reason=(
                diagnostics[need_id]["motiu"]
                if need_id in diagnostics
                else "No determinat"
            ),
            had_reference_assignment=need_id in references,
            static_candidates=(
                diagnostics[need_id]["candidats_estatics"]
                if need_id in diagnostics
                else None
            ),
            compatible_candidates=(
                diagnostics[need_id]["compatibles_amb_proposta"]
                if need_id in diagnostics
                else None
            ),
        )
        for need_id in sorted(
            uncovered_ids,
            key=lambda item: (
                needs_by_id[item].date,
                needs_by_id[item].service_id,
                item,
            ),
        )
    )
    return PlanningProposal(
        prepared=prepared,
        selection=selection,
        unchanged_assignments=tuple(unchanged),
        changes=tuple(changes),
        uncovered_needs=uncovered,
        solver_config=solver_config,
        requested_seeds=requested_seeds,
        force_all_seeds=force_all_seeds,
    )


def generate_planning_proposal(
    prepared: PreparedPlanningProblem,
    *,
    config: SolverConfig | None = None,
    seeds: Iterable[int] = (0, 1, 2),
    force_all_seeds: bool = False,
) -> PlanningProposal:
    """Resol el problema preparat i retorna exclusivament canvis en memòria."""
    if not isinstance(prepared, PreparedPlanningProblem):
        raise TypeError("prepared ha de ser PreparedPlanningProblem")
    solver_config = config or SolverConfig(
        max_time_seconds=60,
        equity_time_seconds=15,
        num_workers=8,
        random_seed=0,
    )
    requested_seeds = tuple(dict.fromkeys(int(seed) for seed in seeds))
    try:
        selection = solve_adaptive_multi_start(
            CpSatPlanner(prepared.problem),
            solver_config,
            requested_seeds,
            force_all_seeds=force_all_seeds,
        )
    except MultiStartSelectionError as error:
        raise PlanningProposalGenerationError(str(error)) from error
    return planning_proposal_from_result(
        prepared,
        selection,
        solver_config=solver_config,
        requested_seeds=requested_seeds,
        force_all_seeds=force_all_seeds,
    )
