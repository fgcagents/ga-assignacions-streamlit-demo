from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import date

from .domain import PlanningProblem, Worker
from .model import CpSatPlanner


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    name: str
    description: str
    start_date: date
    end_date: date
    absent_worker_ids: frozenset[str] = frozenset()
    excluded_worker_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.start_date > self.end_date:
            raise ValueError("La data inicial de l'escenari no pot superar la final")
        overlap = self.absent_worker_ids.intersection(self.excluded_worker_ids)
        if overlap:
            raise ValueError(
                "Un treballador no pot estar absent i exclòs al mateix escenari: "
                + ", ".join(sorted(overlap))
            )


def candidate_opportunities(problem: PlanningProblem) -> Counter[str]:
    """Compta els candidats estàtics que cada treballador podria cobrir."""
    return Counter(worker_id for worker_id, _ in CpSatPlanner(problem).candidate_pairs())


def apply_scenario(
    problem: PlanningProblem,
    scenario: ScenarioSpec,
) -> PlanningProblem:
    """
    Aplica absències i canvis de plantilla només en memòria.

    El problema d'entrada ha d'haver estat carregat per al període de
    l'escenari perquè la política d'històric sigui coherent amb aquell horitzó.
    """
    known_worker_ids = {worker.id for worker in problem.workers}
    requested_ids = scenario.absent_worker_ids.union(scenario.excluded_worker_ids)
    unknown_ids = requested_ids.difference(known_worker_ids)
    if unknown_ids:
        raise ValueError(
            "Treballadors inexistents a l'escenari: "
            + ", ".join(sorted(unknown_ids))
        )

    needs = tuple(
        need
        for need in problem.needs
        if scenario.start_date <= need.date <= scenario.end_date
    )
    horizon_dates = frozenset(need.date for need in needs)
    workers = tuple(
        replace(
            worker,
            rest_dates=worker.rest_dates.union(horizon_dates),
        )
        if worker.id in scenario.absent_worker_ids
        else worker
        for worker in problem.workers
        if worker.id not in scenario.excluded_worker_ids
    )

    return PlanningProblem(
        workers=workers,
        needs=needs,
        history=tuple(
            assignment
            for assignment in problem.history
            if assignment.worker_id not in scenario.excluded_worker_ids
        ),
        exclusions=frozenset(
            exclusion
            for exclusion in problem.exclusions
            if exclusion[0] not in scenario.excluded_worker_ids
        ),
    )


def build_standard_scenarios(problem: PlanningProblem) -> tuple[ScenarioSpec, ...]:
    """Construeix la bateria reproduïble del punt 7."""
    dates = sorted({need.date for need in problem.needs})
    if len(dates) < 2:
        raise ValueError("Calen almenys dos dies de necessitats per crear períodes")

    t_workers = tuple(worker for worker in problem.workers if worker.group == "T")
    opportunities = candidate_opportunities(problem)

    def ranked(workers: tuple[Worker, ...]) -> list[Worker]:
        return sorted(
            workers,
            key=lambda worker: (-opportunities[worker.id], worker.id),
        )

    ge_workers = ranked(
        tuple(worker for worker in t_workers if "GE" in worker.skills)
    )
    ae_only_workers = ranked(
        tuple(
            worker
            for worker in t_workers
            if "AE" in worker.skills and "GE" not in worker.skills
        )
    )
    if len(ge_workers) < 3 or len(ae_only_workers) < 2:
        raise ValueError(
            "La bateria estàndard requereix almenys 3 treballadors GE "
            "i 2 treballadors exclusivament AE"
        )

    split_index = len(dates) // 2
    first_end = dates[split_index - 1]
    second_start = dates[split_index]
    strongest_ge = ge_workers[0].id
    reduced_ids = frozenset(
        {strongest_ge, ae_only_workers[0].id, ae_only_workers[1].id}
    )
    scarce_ge_ids = frozenset(worker.id for worker in ge_workers[:3])

    return (
        ScenarioSpec(
            name="base_completa",
            description="Període complet i plantilla original",
            start_date=dates[0],
            end_date=dates[-1],
        ),
        ScenarioSpec(
            name="primera_meitat",
            description="Primera meitat de l'horitzó",
            start_date=dates[0],
            end_date=first_end,
        ),
        ScenarioSpec(
            name="segona_meitat",
            description="Segona meitat de l'horitzó",
            start_date=second_start,
            end_date=dates[-1],
        ),
        ScenarioSpec(
            name="baixa_llarga_ge",
            description=(
                "Baixa durant tot l'horitzó del GE amb més oportunitats: "
                f"{strongest_ge}"
            ),
            start_date=dates[0],
            end_date=dates[-1],
            absent_worker_ids=frozenset({strongest_ge}),
        ),
        ScenarioSpec(
            name="plantilla_reduida",
            description=(
                "Plantilla reduïda en 1 GE i 2 AE amb moltes oportunitats: "
                + ", ".join(sorted(reduced_ids))
            ),
            start_date=dates[0],
            end_date=dates[-1],
            excluded_worker_ids=reduced_ids,
        ),
        ScenarioSpec(
            name="habilitacio_ge_escassa",
            description=(
                "Retirada dels 3 GE amb més oportunitats: "
                + ", ".join(sorted(scarce_ge_ids))
            ),
            start_date=dates[0],
            end_date=dates[-1],
            excluded_worker_ids=scarce_ge_ids,
        ),
    )
