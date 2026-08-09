"""Diagnòstic funcional reproduïble d'una solució CP-SAT."""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from typing import Iterable

from .domain import (
    MINIMUM_REST_MINUTES,
    Assignment,
    Need,
    PlanningProblem,
    SolveResult,
    assignments_compatible,
)
from .model import CpSatPlanner


def _profile(
    needs: Iterable[Need],
    covered_ids: set[str],
    assignments: tuple[Assignment, ...],
) -> dict:
    selected = tuple(needs)
    selected_ids = {need.id for need in selected}
    assigned_workers = {
        assignment.worker_id
        for assignment in assignments
        if assignment.need_id in selected_ids
    }
    total_minutes = sum(need.duration_minutes for need in selected)
    return {
        "necessitats": len(selected),
        "cobertes": len(selected_ids & covered_ids),
        "descobertes": len(selected_ids - covered_ids),
        "hores_totals": round(total_minutes / 60, 2),
        "durada_mitjana_hores": (
            round(total_minutes / 60 / len(selected), 2) if selected else 0.0
        ),
        "treballadors_assignats": len(assigned_workers),
    }


def _uncovered_diagnostic(
    problem: PlanningProblem,
    need: Need,
    assignments: tuple[Assignment, ...],
    planner: CpSatPlanner,
) -> dict:
    workers_t = [worker for worker in problem.workers if worker.group == "T"]
    skilled = [
        worker
        for worker in workers_t
        if need.required_skills.intersection(worker.skills)
    ]
    not_excluded = [
        worker
        for worker in skilled
        if (worker.id, need.date) not in problem.exclusions
    ]
    not_resting = [
        worker for worker in not_excluded if need.date not in worker.rest_dates
    ]
    within_annual_limit = [
        worker
        for worker in not_resting
        if need.duration_minutes <= worker.remaining_annual_minutes
    ]
    history_compatible = [
        worker
        for worker in within_annual_limit
        if all(
            assignments_compatible(
                need.start,
                need.end,
                historical.start,
                historical.end,
            )
            for historical in planner.history_by_worker.get(worker.id, ())
        )
    ]
    static_candidates = [
        worker for worker in workers_t if planner.is_static_candidate(worker, need)
    ]
    occupied_days = {
        (assignment.worker_id, assignment.date) for assignment in assignments
    }
    free_same_day = [
        worker
        for worker in static_candidates
        if (worker.id, need.date) not in occupied_days
    ]
    assignments_by_worker: dict[str, list[Assignment]] = defaultdict(list)
    for assignment in assignments:
        assignments_by_worker[assignment.worker_id].append(assignment)
    compatible_with_proposal = [
        worker
        for worker in free_same_day
        if all(
            assignments_compatible(
                need.start,
                need.end,
                assignment.start,
                assignment.end,
            )
            for assignment in assignments_by_worker.get(worker.id, ())
        )
    ]

    if not skilled:
        reason = "Sense personal T amb habilitació compatible"
    elif not not_excluded:
        reason = "Tots els candidats habilitats estan exclosos"
    elif not not_resting:
        reason = "Tots els candidats habilitats tenen descans"
    elif not within_annual_limit:
        reason = "Tots els candidats disponibles superen el límit anual"
    elif not history_compatible:
        reason = "Cap candidat compleix les 12 hores respecte de l'històric"
    elif not free_same_day:
        reason = "Tots els candidats estàtics ja treballen aquell dia"
    elif not compatible_with_proposal:
        reason = "Cap candidat lliure compleix les 12 hores amb la proposta"
    else:
        reason = (
            "Conflicte global de combinació o capacitat; hi ha candidat local"
        )

    return {
        "necessitat_id": need.id,
        "data": need.date.isoformat(),
        "servei": need.service_id,
        "habilitacions": ",".join(sorted(need.required_skills)),
        "zona": need.zone,
        "motiu": reason,
        "treballadors_t": len(workers_t),
        "habilitats": len(skilled),
        "despres_exclusions": len(not_excluded),
        "despres_descansos": len(not_resting),
        "despres_limit_anual": len(within_annual_limit),
        "despres_historic_12h": len(history_compatible),
        "candidats_estatics": len(static_candidates),
        "lliures_persona_dia": len(free_same_day),
        "compatibles_amb_proposta": len(compatible_with_proposal),
    }


def _rest_diagnostic(
    problem: PlanningProblem,
    assignments: tuple[Assignment, ...],
) -> dict:
    proposal_by_worker: dict[str, list[Assignment]] = defaultdict(list)
    for assignment in assignments:
        proposal_by_worker[assignment.worker_id].append(assignment)
    history_by_worker = defaultdict(list)
    for historical in problem.history:
        history_by_worker[historical.worker_id].append(historical)

    gaps: list[int] = []
    violations = 0
    exact = 0
    for worker_id, proposed in proposal_by_worker.items():
        pairs = list(combinations(proposed, 2))
        pairs.extend(
            (proposal, historical)
            for proposal in proposed
            for historical in history_by_worker.get(worker_id, ())
        )
        for first, second in pairs:
            if first.end <= second.start:
                gap = int((second.start - first.end).total_seconds() // 60)
            elif second.end <= first.start:
                gap = int((first.start - second.end).total_seconds() // 60)
            else:
                gap = -1
            gaps.append(gap)
            if gap < MINIMUM_REST_MINUTES:
                violations += 1
            elif gap == MINIMUM_REST_MINUTES:
                exact += 1
    return {
        "parelles_comprovades": len(gaps),
        "descans_minim_observat_hores": (
            round(min(gaps) / 60, 2) if gaps else None
        ),
        "parelles_exactament_12h": exact,
        "violacions": violations,
    }


def analyze_functional_result(
    problem: PlanningProblem,
    result: SolveResult,
    *,
    worker_names: dict[str, str] | None = None,
) -> dict:
    """Resumeix els riscos funcionals prioritaris d'una solució real."""
    names = worker_names or {}
    assignments = tuple(result.assignments)
    covered_ids = {assignment.need_id for assignment in assignments}
    planner = CpSatPlanner(problem)
    needs_by_id = {need.id: need for need in problem.needs}
    candidate_counts: dict[str, int] = defaultdict(int)
    for _, need_id in planner.candidate_pairs():
        candidate_counts[need_id] += 1

    skill_groups: dict[str, list[Need]] = defaultdict(list)
    for need in problem.needs:
        profile = "+".join(sorted(need.required_skills)) or "sense_habilitacio"
        skill_groups[profile].append(need)
    workers_t = [worker for worker in problem.workers if worker.group == "T"]
    skill_profiles = []
    for profile, needs in sorted(skill_groups.items()):
        required = needs[0].required_skills
        workers_with_skill = sum(
            bool(required.intersection(worker.skills)) for worker in workers_t
        )
        counts = [candidate_counts[need.id] for need in needs]
        skill_profiles.append(
            {
                "perfil": profile,
                "necessitats": len(needs),
                "descobertes": sum(
                    need.id not in covered_ids for need in needs
                ),
                "treballadors_amb_habilitacio": workers_with_skill,
                "candidats_estatics_minim": min(counts),
                "candidats_estatics_mitjana": round(
                    sum(counts) / len(counts), 2
                ),
                "candidats_estatics_maxim": max(counts),
            }
        )

    assigned_minutes: dict[str, int] = defaultdict(int)
    assigned_counts: dict[str, int] = defaultdict(int)
    for assignment in assignments:
        assigned_minutes[assignment.worker_id] += assignment.duration_minutes
        assigned_counts[assignment.worker_id] += 1
    worker_loads = []
    overloaded = []
    without_assignments = []
    for worker in workers_t:
        final_minutes = worker.annual_minutes + assigned_minutes[worker.id]
        item = {
            "treballador_id": worker.id,
            "treballador": names.get(worker.id, worker.id),
            "assignacions": assigned_counts[worker.id],
            "hores_periode": round(assigned_minutes[worker.id] / 60, 2),
            "hores_anuals_abans": round(worker.annual_minutes / 60, 2),
            "hores_anuals_despres": round(final_minutes / 60, 2),
            "limit_anual_hores": round(worker.max_annual_minutes / 60, 2),
            "percentatge_limit": round(
                100 * final_minutes / worker.max_annual_minutes, 1
            )
            if worker.max_annual_minutes
            else None,
            "candidatures_estatiques": sum(
                planner.is_static_candidate(worker, need)
                for need in problem.needs
            ),
        }
        worker_loads.append(item)
        if item["assignacions"] == 0:
            without_assignments.append(item)
        if final_minutes > worker.max_annual_minutes:
            overloaded.append(item)

    uncovered_needs = [
        need for need in problem.needs if need.id not in covered_ids
    ]
    final_annual_hours = [
        (worker.annual_minutes + assigned_minutes[worker.id]) / 60
        for worker in workers_t
    ]
    coverage_phase = (
        result.optimization_phases[0] if result.optimization_phases else None
    )
    metrics = result.soft_metrics
    return {
        "periode": {
            "inici": min(need.date for need in problem.needs).isoformat(),
            "fi": max(need.date for need in problem.needs).isoformat(),
        },
        "solver": {
            "estat": result.status,
            "fase_cobertura": (
                coverage_phase.status if coverage_phase else "NO_EXECUTADA"
            ),
            "gap_cobertura": (
                coverage_phase.relative_gap if coverage_phase else None
            ),
            "temps_segons": round(result.wall_time_seconds, 3),
            "errors_validador": list(result.validation_errors),
            "fases": [
                {
                    "nom": phase.name,
                    "estat": phase.status,
                    "objectiu": phase.objective_value,
                    "cota": phase.best_objective_bound,
                    "gap": phase.relative_gap,
                    "temps_segons": round(phase.wall_time_seconds, 3),
                }
                for phase in result.optimization_phases
            ],
        },
        "cobertura": {
            "necessitats": result.total_needs,
            "cobertes": result.covered_needs,
            "descobertes": result.total_needs - result.covered_needs,
            "percentatge": round(
                100 * result.covered_needs / result.total_needs, 2
            )
            if result.total_needs
            else 100.0,
        },
        "serveis_ge": _profile(
            (
                need
                for need in problem.needs
                if "GE" in {skill.upper() for skill in need.required_skills}
            ),
            covered_ids,
            assignments,
        ),
        "horaris_0": _profile(
            (need for need in problem.needs if need.service_id.endswith("0")),
            covered_ids,
            assignments,
        ),
        "perfils_habilitacio": skill_profiles,
        "descans_12h": _rest_diagnostic(problem, assignments),
        "equitat_oportunista": {
            "rang_hores_anuals_model": (
                round(metrics.annual_hours_range_minutes / 60, 2)
                if metrics
                else None
            ),
            "canvis_zona": metrics.zone_changes if metrics else None,
            "canvis_torn": metrics.turn_changes if metrics else None,
        },
        "carrega": {
            "treballadors_t": len(workers_t),
            "treballadors_assignats": sum(
                bool(item["assignacions"]) for item in worker_loads
            ),
            "sense_assignacions": sorted(
                without_assignments,
                key=lambda item: item["treballador"],
            ),
            "sobrecarregats": overloaded,
            "a_partir_90_percent_limit": [
                item
                for item in worker_loads
                if item["percentatge_limit"] is not None
                and item["percentatge_limit"] >= 90
            ],
            "carrega_maxima_periode": max(
                (item["hores_periode"] for item in worker_loads), default=0.0
            ),
            "carrega_minima_assignats": min(
                (
                    item["hores_periode"]
                    for item in worker_loads
                    if item["assignacions"]
                ),
                default=0.0,
            ),
            "hores_anuals_minimes_despres": round(
                min(final_annual_hours), 2
            ),
            "hores_anuals_maximes_despres": round(
                max(final_annual_hours), 2
            ),
            "rang_hores_anuals_despres": round(
                max(final_annual_hours) - min(final_annual_hours), 2
            ),
            "detall": sorted(
                worker_loads,
                key=lambda item: (-item["hores_periode"], item["treballador"]),
            ),
        },
        "diagnostic_descobertes": [
            _uncovered_diagnostic(
                problem,
                need,
                assignments,
                planner,
            )
            for need in uncovered_needs
        ],
        "assignacions_desconegudes": [
            assignment.need_id
            for assignment in assignments
            if assignment.need_id not in needs_by_id
        ],
    }
