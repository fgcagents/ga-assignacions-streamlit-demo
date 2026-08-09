from __future__ import annotations

from collections import defaultdict
from datetime import date
from itertools import combinations
from typing import Iterable

from ortools.sat.python import cp_model

from ..domain import (
    Assignment,
    HistoricalAssignment,
    Need,
    PlanningProblem,
    Worker,
    assignments_compatible,
    group_history_by_worker,
)
from .types import CoreModel


class HardConstraintSet:
    """Construcció i validació de totes les regles obligatòries."""

    def __init__(self, problem: PlanningProblem):
        self.problem = problem
        self.workers = {worker.id: worker for worker in problem.workers}
        self.needs = {need.id: need for need in problem.needs}
        self.history_by_worker: dict[
            str, tuple[HistoricalAssignment, ...]
        ] = group_history_by_worker(problem.history)

    def is_static_candidate(self, worker: Worker, need: Need) -> bool:
        """Aplica les regles individuals abans de crear cap variable."""
        if worker.group != "T":
            return False
        if (worker.id, need.date) in self.problem.exclusions:
            return False
        if need.date in worker.rest_dates:
            return False
        if not need.required_skills.intersection(worker.skills):
            return False
        if need.duration_minutes > worker.remaining_annual_minutes:
            return False
        return all(
            assignments_compatible(
                need.start,
                need.end,
                historical.start,
                historical.end,
            )
            for historical in self.history_by_worker.get(worker.id, ())
        )

    def candidate_pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (worker.id, need.id)
            for need in self.problem.needs
            for worker in self.problem.workers
            if self.is_static_candidate(worker, need)
        )

    def build_core_model(self) -> CoreModel:
        """Crea variables i aplica totes les restriccions rígides."""
        model = cp_model.CpModel()
        candidate_pairs = self.candidate_pairs()
        assignment_vars = {
            pair: model.new_bool_var(f"x__{pair[0]}__{pair[1]}")
            for pair in candidate_pairs
        }
        vars_by_need: dict[str, list[cp_model.IntVar]] = defaultdict(list)
        vars_by_worker_day: dict[
            tuple[str, date], list[cp_model.IntVar]
        ] = defaultdict(list)
        need_ids_by_worker: dict[str, list[str]] = defaultdict(list)

        for (worker_id, need_id), variable in assignment_vars.items():
            need = self.needs[need_id]
            vars_by_need[need_id].append(variable)
            vars_by_worker_day[(worker_id, need.date)].append(variable)
            need_ids_by_worker[worker_id].append(need_id)

        coverage_vars = self._add_need_coverage(
            model, vars_by_need
        )
        self._add_locked_assignments(model, assignment_vars)
        self._add_person_day(model, vars_by_worker_day)
        incompatibility_constraints = self._add_pair_compatibility(
            model, assignment_vars, need_ids_by_worker
        )
        self._add_annual_hours(
            model, assignment_vars, need_ids_by_worker
        )

        return CoreModel(
            model=model,
            candidate_pairs=candidate_pairs,
            assignment_vars=assignment_vars,
            coverage_vars=coverage_vars,
            vars_by_worker_day=vars_by_worker_day,
            need_ids_by_worker=need_ids_by_worker,
            incompatibility_constraints=incompatibility_constraints,
        )

    def _add_need_coverage(
        self,
        model: cp_model.CpModel,
        vars_by_need: dict[str, list[cp_model.IntVar]],
    ) -> dict[str, cp_model.IntVar]:
        coverage_vars: dict[str, cp_model.IntVar] = {}
        for need in self.problem.needs:
            coverage = model.new_bool_var(f"covered__{need.id}")
            coverage_vars[need.id] = coverage
            candidates = vars_by_need.get(need.id, [])
            model.add(
                sum(candidates) == coverage
                if candidates
                else coverage == 0
            )
        return coverage_vars

    def _add_locked_assignments(
        self,
        model: cp_model.CpModel,
        assignment_vars: dict[tuple[str, str], cp_model.IntVar],
    ) -> None:
        reference_by_need = {
            assignment.need_id: assignment
            for assignment in self.problem.reference_assignments
        }
        for need_id in self.problem.locked_need_ids:
            reference = reference_by_need[need_id]
            variable = assignment_vars.get((reference.worker_id, need_id))
            if variable is None:
                raise ValueError(
                    "L'assignació bloquejada ja no compleix les "
                    "restriccions dures: "
                    f"{reference.worker_id} -> {need_id}"
                )
            model.add(variable == 1)

    @staticmethod
    def _add_person_day(
        model: cp_model.CpModel,
        vars_by_worker_day: dict[
            tuple[str, date], list[cp_model.IntVar]
        ],
    ) -> None:
        for variables in vars_by_worker_day.values():
            if len(variables) > 1:
                model.add_at_most_one(variables)

    def _add_pair_compatibility(
        self,
        model: cp_model.CpModel,
        assignment_vars: dict[tuple[str, str], cp_model.IntVar],
        need_ids_by_worker: dict[str, list[str]],
    ) -> int:
        count = 0
        for worker_id, need_ids in need_ids_by_worker.items():
            unique_need_ids = tuple(dict.fromkeys(need_ids))
            for first_id, second_id in combinations(unique_need_ids, 2):
                first = self.needs[first_id]
                second = self.needs[second_id]
                if first.date == second.date:
                    continue
                if not assignments_compatible(
                    first.start,
                    first.end,
                    second.start,
                    second.end,
                ):
                    model.add(
                        assignment_vars[(worker_id, first_id)]
                        + assignment_vars[(worker_id, second_id)]
                        <= 1
                    )
                    count += 1
        return count

    def _add_annual_hours(
        self,
        model: cp_model.CpModel,
        assignment_vars: dict[tuple[str, str], cp_model.IntVar],
        need_ids_by_worker: dict[str, list[str]],
    ) -> None:
        for worker_id, need_ids in need_ids_by_worker.items():
            worker = self.workers[worker_id]
            model.add(
                sum(
                    self.needs[need_id].duration_minutes
                    * assignment_vars[(worker_id, need_id)]
                    for need_id in dict.fromkeys(need_ids)
                )
                <= worker.remaining_annual_minutes
            )

    def validate(self, assignments: Iterable[Assignment]) -> list[str]:
        """Valida amb les mateixes regles, independentment del solver."""
        errors: list[str] = []
        by_need: dict[str, list[Assignment]] = defaultdict(list)
        by_worker_day: dict[
            tuple[str, date], list[Assignment]
        ] = defaultdict(list)
        by_worker: dict[str, list[Assignment]] = defaultdict(list)

        for assignment in tuple(assignments):
            worker = self.workers.get(assignment.worker_id)
            need = self.needs.get(assignment.need_id)
            if worker is None:
                errors.append(
                    f"Treballador desconegut: {assignment.worker_id}"
                )
                continue
            if need is None:
                errors.append(
                    f"Necessitat desconeguda: {assignment.need_id}"
                )
                continue
            if not self.is_static_candidate(worker, need):
                errors.append(
                    "Candidat invàlid: "
                    f"{assignment.worker_id} -> {assignment.need_id}"
                )
            by_need[assignment.need_id].append(assignment)
            by_worker_day[
                (assignment.worker_id, assignment.date)
            ].append(assignment)
            by_worker[assignment.worker_id].append(assignment)

        self._validate_need_once(by_need, errors)
        self._validate_locked(by_need, errors)
        self._validate_person_day(by_worker_day, errors)
        self._validate_compatibility_and_hours(by_worker, errors)
        return errors

    @staticmethod
    def _validate_need_once(
        by_need: dict[str, list[Assignment]],
        errors: list[str],
    ) -> None:
        for need_id, assigned in by_need.items():
            if len(assigned) > 1:
                errors.append(
                    f"Necessitat assignada més d'una vegada: {need_id}"
                )

    def _validate_locked(
        self,
        by_need: dict[str, list[Assignment]],
        errors: list[str],
    ) -> None:
        reference_by_need = {
            assignment.need_id: assignment
            for assignment in self.problem.reference_assignments
        }
        for need_id in self.problem.locked_need_ids:
            reference = reference_by_need[need_id]
            assigned = by_need.get(need_id, [])
            if (
                len(assigned) != 1
                or assigned[0].worker_id != reference.worker_id
            ):
                errors.append(
                    "Assignació bloquejada no respectada: "
                    f"{reference.worker_id} -> {need_id}"
                )

    @staticmethod
    def _validate_person_day(
        by_worker_day: dict[tuple[str, date], list[Assignment]],
        errors: list[str],
    ) -> None:
        for (worker_id, day), assigned in by_worker_day.items():
            if len(assigned) > 1:
                errors.append(
                    "Més d'una assignació el mateix dia: "
                    f"{worker_id} / {day}"
                )

    def _validate_compatibility_and_hours(
        self,
        by_worker: dict[str, list[Assignment]],
        errors: list[str],
    ) -> None:
        for worker_id, assigned in by_worker.items():
            for first, second in combinations(assigned, 2):
                if not assignments_compatible(
                    first.start,
                    first.end,
                    second.start,
                    second.end,
                ):
                    errors.append(
                        "Solapament o descans inferior a 12 h: "
                        f"{worker_id} / {first.need_id} / "
                        f"{second.need_id}"
                    )
            used_minutes = sum(
                item.duration_minutes for item in assigned
            )
            if (
                used_minutes
                > self.workers[worker_id].remaining_annual_minutes
            ):
                errors.append(f"Límit anual superat: {worker_id}")
