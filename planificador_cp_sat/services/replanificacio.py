"""Pont de només lectura entre les incidències i el pilot CP-SAT."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PILOT_SRC = PROJECT_ROOT / "cp_sat_pilot" / "src"
if str(PILOT_SRC) not in sys.path:
    sys.path.insert(0, str(PILOT_SRC))

from cp_sat_pilot import (  # noqa: E402
    Assignment,
    CpSatPlanner,
    Need,
    PlanningProblem,
    SolveResult,
    SolverConfig,
)
from cp_sat_pilot.sqlite_adapter import (  # noqa: E402
    load_problem_from_sqlite,
)
from planificador_cp_sat.domain import (  # noqa: E402
    PlanningExecutionRequest,
    PlanningInputAdjustments,
    PlanningScope,
    PlanningTrigger,
    PlanningTriggerKind,
)
from planificador_cp_sat.services.preparacio_planificacio import (  # noqa: E402
    PreparedPlanningProblem,
    prepare_planning_problem,
)
from planificador_cp_sat.services.proposta_planificacio import (  # noqa: E402
    PlanningProposal,
    generate_planning_proposal,
)


TIPUS_CP_SAT = frozenset(
    {
        "baixa",
        "vacances",
        "prorroga_baixa",
        "substitucio",
        "alta_anticipada",
    }
)


@dataclass(frozen=True, slots=True)
class PlanSource:
    assignment_id: int
    need_id: str
    worker_id: str
    state: str
    date: date
    turn: str


@dataclass(frozen=True, slots=True)
class IncidentPlanningContext:
    incidence_id: int
    incident_type: str
    start_date: date
    end_date: date
    problem: PlanningProblem
    sources: tuple[PlanSource, ...]
    snapshot_hash: str
    prepared: PreparedPlanningProblem


@dataclass(frozen=True, slots=True)
class IncidentCpSatDraft:
    context: IncidentPlanningContext
    result: SolveResult | None
    changes: tuple[dict[str, Any], ...]
    proposal: PlanningProposal | None = None


def _readonly_connection(database_path: str | Path) -> sqlite3.Connection:
    resolved = Path(database_path).resolve()
    encoded = quote(resolved.as_posix(), safe="/:")
    connection = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _split_skills(raw_value: object) -> frozenset[str]:
    return frozenset(
        part.strip().upper()
        for part in str(raw_value or "")
        .replace("+", ",")
        .replace(";", ",")
        .split(",")
        if part.strip()
    )


def _interval_from_plan(row: sqlite3.Row) -> tuple[datetime, datetime]:
    try:
        start = datetime.fromisoformat(f"{row['data']}T{row['hora_inici']}")
        end = datetime.fromisoformat(f"{row['data']}T{row['hora_fi']}")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Horari invàlid a l'assignació publicada #{row['id']}"
        ) from exc
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _fallback_need(row: sqlite3.Row) -> Need:
    start, end = _interval_from_plan(row)
    day = date.fromisoformat(row["data"])
    turn = str(row["torn"])
    return Need(
        id=f"{day.isoformat()}::{turn}",
        service_id=turn,
        date=day,
        start=start,
        end=end,
        required_skills=_split_skills(row["formacio"]),
        zone=str(row["zona"] or ""),
        turn_options=frozenset(),
    )


def plan_snapshot_hash(rows: list[sqlite3.Row]) -> str:
    payload = [
        {
            "id": row["id"],
            "data": row["data"],
            "torn": row["torn"],
            "treballador_id": str(row["treballador_id"]),
            "hora_inici": row["hora_inici"],
            "hora_fi": row["hora_fi"],
            "durada_hores": row["durada_hores"],
            "formacio": row["formacio"],
            "linia": row["linia"],
            "zona": row["zona"],
            "estat_planificacio": row["estat_planificacio"],
        }
        for row in rows
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prepare_incident_problem_legacy(
    database_path: str | Path,
    incidence_id: int,
) -> IncidentPlanningContext:
    """Construeix una fotografia immutable del pla sense modificar SQLite."""
    with closing(_readonly_connection(database_path)) as connection:
        incidence = connection.execute(
            "SELECT * FROM incidencies_personal WHERE id = ?",
            (incidence_id,),
        ).fetchone()
        if incidence is None:
            raise ValueError("No s'ha trobat la incidència")
        if incidence["tipus"] not in TIPUS_CP_SAT:
            raise ValueError(
                f"El tipus {incidence['tipus']} encara no usa CP-SAT"
            )
        start_date = date.fromisoformat(incidence["data_inici"])
        end_date = date.fromisoformat(incidence["data_fi"])
        released_dates: set[date] = set()
        if incidence["tipus"] == "alta_anticipada":
            last_sick_day = connection.execute(
                """
                SELECT MAX(data) FROM descansos_dies
                WHERE CAST(treballador_id AS TEXT) = CAST(? AS TEXT)
                  AND origen = 'baixa' AND data >= ?
                """,
                (incidence["treballador_id"], start_date.isoformat()),
            ).fetchone()[0]
            if last_sick_day:
                end_date = max(end_date, date.fromisoformat(last_sick_day))
            released_dates = {
                date.fromisoformat(row[0])
                for row in connection.execute(
                    """
                    SELECT data FROM descansos_dies
                    WHERE CAST(treballador_id AS TEXT) = CAST(? AS TEXT)
                      AND origen = 'baixa' AND data BETWEEN ? AND ?
                    """,
                    (
                        incidence["treballador_id"],
                        start_date.isoformat(),
                        end_date.isoformat(),
                    ),
                ).fetchall()
            }
        substitute_id = (
            str(incidence["treballador_substitut_id"])
            if incidence["treballador_substitut_id"] is not None
            else None
        )
        if incidence["tipus"] == "substitucio":
            if substitute_id is None:
                raise ValueError("La substitució no té treballador substitut")
            conflict = connection.execute(
                """
                SELECT 1 FROM descansos_dies
                WHERE CAST(treballador_id AS TEXT) = CAST(? AS TEXT)
                  AND data BETWEEN ? AND ?
                  AND COALESCE(origen, '') <> 'base'
                LIMIT 1
                """,
                (
                    substitute_id,
                    start_date.isoformat(),
                    end_date.isoformat(),
                ),
            ).fetchone()
            active_substitution = connection.execute(
                """
                SELECT 1 FROM descansos_dies
                WHERE CAST(treballador_substitut_id AS TEXT) = CAST(? AS TEXT)
                  AND data BETWEEN ? AND ?
                  AND CAST(treballador_id AS TEXT) <> CAST(? AS TEXT)
                LIMIT 1
                """,
                (
                    substitute_id,
                    start_date.isoformat(),
                    end_date.isoformat(),
                    incidence["treballador_id"],
                ),
            ).fetchone()
            if conflict or active_substitution:
                raise ValueError(
                    "El substitut indicat té una indisponibilitat no substituïble"
                )
        plan_rows = connection.execute(
            """
            SELECT id, data, torn, treballador_id, hora_inici, hora_fi,
                   durada_hores, formacio, linia, zona, estat_planificacio
            FROM assig_grup_T
            WHERE data BETWEEN ? AND ?
              AND estat_planificacio IN ('publicada', 'bloquejada')
            ORDER BY data, torn, id
            """,
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
        snapshot_rows = connection.execute(
            """
            SELECT id, data, torn, treballador_id, hora_inici, hora_fi,
                   durada_hores, formacio, linia, zona, estat_planificacio
            FROM assig_grup_T
            WHERE data BETWEEN ? AND ?
              AND estat_planificacio IN ('publicada', 'bloquejada')
            ORDER BY data, torn, id
            """,
            (
                (start_date - timedelta(days=2)).isoformat(),
                (end_date + timedelta(days=2)).isoformat(),
            ),
        ).fetchall()

    base_problem = load_problem_from_sqlite(
        database_path,
        start_date=start_date,
        end_date=end_date,
        duplicate_policy="replace_all",
        allow_empty_needs=True,
    )
    base_needs = {need.id: need for need in base_problem.needs}
    selected_needs: list[Need] = []
    references: list[Assignment] = []
    sources: list[PlanSource] = []
    affected_need_ids: set[str] = set()
    locked_need_ids: set[str] = set()
    seen_need_ids: set[str] = set()
    affected_worker_id = str(incidence["treballador_id"])

    for row in plan_rows:
        day = date.fromisoformat(row["data"])
        need_id = f"{day.isoformat()}::{row['torn']}"
        if need_id in seen_need_ids:
            raise ValueError(
                f"El pla publicat conté més d'una assignació activa per {need_id}"
            )
        seen_need_ids.add(need_id)
        need = base_needs.get(need_id) or _fallback_need(row)
        selected_needs.append(need)
        worker_id = str(row["treballador_id"])
        references.append(
            Assignment(
                worker_id=worker_id,
                need_id=need.id,
                service_id=need.service_id,
                date=need.date,
                start=need.start,
                end=need.end,
                duration_minutes=need.duration_minutes,
            )
        )
        sources.append(
            PlanSource(
                assignment_id=int(row["id"]),
                need_id=need_id,
                worker_id=worker_id,
                state=str(row["estat_planificacio"]),
                date=day,
                turn=str(row["torn"]),
            )
        )
        if (
            worker_id == affected_worker_id
            and incidence["tipus"] != "alta_anticipada"
        ):
            affected_need_ids.add(need_id)
        elif row["estat_planificacio"] == "bloquejada":
            locked_need_ids.add(need_id)

    if incidence["tipus"] == "alta_anticipada":
        for need in base_problem.needs:
            if need.id not in seen_need_ids:
                selected_needs.append(need)
                seen_need_ids.add(need.id)

    exclusions = set(base_problem.exclusions)
    workers = base_problem.workers
    if incidence["tipus"] == "alta_anticipada":
        workers = tuple(
            replace(
                worker,
                rest_dates=worker.rest_dates - released_dates,
            )
            if worker.id == affected_worker_id
            else worker
            for worker in workers
        )
        exclusions = {
            exclusion
            for exclusion in exclusions
            if not (
                exclusion[0] == affected_worker_id
                and exclusion[1] in released_dates
            )
        }
    else:
        current_day = start_date
        while current_day <= end_date:
            exclusions.add((affected_worker_id, current_day))
            current_day += timedelta(days=1)

    if incidence["tipus"] == "substitucio" and substitute_id is not None:
        substitution_dates = {
            start_date + timedelta(days=offset)
            for offset in range((end_date - start_date).days + 1)
        }
        workers = tuple(
            replace(
                worker,
                rest_dates=worker.rest_dates - substitution_dates,
            )
            if worker.id == substitute_id
            else worker
            for worker in workers
        )
        exclusions = {
            exclusion
            for exclusion in exclusions
            if not (
                exclusion[0] == substitute_id
                and exclusion[1] in substitution_dates
            )
        }

    preferred_assignments = ()
    if incidence["tipus"] == "substitucio" and substitute_id is not None:
        preferred_assignments = tuple(
            (need_id, substitute_id)
            for need_id in sorted(affected_need_ids)
        )

    problem = PlanningProblem(
        workers=workers,
        needs=tuple(selected_needs),
        history=base_problem.history,
        exclusions=frozenset(exclusions),
        reference_assignments=tuple(references),
        locked_need_ids=frozenset(locked_need_ids),
        affected_need_ids=frozenset(affected_need_ids),
        preferred_assignments=preferred_assignments,
    )
    return IncidentPlanningContext(
        incidence_id=incidence_id,
        incident_type=str(incidence["tipus"]),
        start_date=start_date,
        end_date=end_date,
        problem=problem,
        sources=tuple(sources),
        snapshot_hash=plan_snapshot_hash(snapshot_rows),
        prepared=PreparedPlanningProblem(
            request=PlanningExecutionRequest(
                scope=PlanningScope(start_date, end_date),
            ),
            snapshot=prepare_planning_problem(
                database_path,
                PlanningExecutionRequest(
                    scope=PlanningScope(start_date, end_date),
                ),
            ).snapshot,
            classification=prepare_planning_problem(
                database_path,
                PlanningExecutionRequest(
                    scope=PlanningScope(start_date, end_date),
                ),
            ).classification,
            problem=problem,
        ),
    )


def prepare_incident_problem(
    database_path: str | Path,
    incidence_id: int,
) -> IncidentPlanningContext:
    """Adapta una incidència al contracte i constructor genèrics."""
    with closing(_readonly_connection(database_path)) as connection:
        incidence = connection.execute(
            "SELECT * FROM incidencies_personal WHERE id = ?",
            (incidence_id,),
        ).fetchone()
        if incidence is None:
            raise ValueError("No s'ha trobat la incidència")
        incident_type = str(incidence["tipus"])
        if incident_type not in TIPUS_CP_SAT:
            raise ValueError(f"El tipus {incident_type} encara no usa CP-SAT")

        start_date = date.fromisoformat(incidence["data_inici"])
        end_date = date.fromisoformat(incidence["data_fi"])
        affected_worker_id = str(incidence["treballador_id"])
        released_dates: set[date] = set()
        if incident_type == "alta_anticipada":
            last_sick_day = connection.execute(
                """
                SELECT MAX(data) FROM descansos_dies
                WHERE CAST(treballador_id AS TEXT) = CAST(? AS TEXT)
                  AND origen = 'baixa' AND data >= ?
                """,
                (affected_worker_id, start_date.isoformat()),
            ).fetchone()[0]
            if last_sick_day:
                end_date = max(end_date, date.fromisoformat(last_sick_day))
            released_dates = {
                date.fromisoformat(row[0])
                for row in connection.execute(
                    """
                    SELECT data FROM descansos_dies
                    WHERE CAST(treballador_id AS TEXT) = CAST(? AS TEXT)
                      AND origen = 'baixa' AND data BETWEEN ? AND ?
                    """,
                    (
                        affected_worker_id,
                        start_date.isoformat(),
                        end_date.isoformat(),
                    ),
                )
            }

        substitute_id = (
            str(incidence["treballador_substitut_id"])
            if incidence["treballador_substitut_id"] is not None
            else None
        )
        if incident_type == "substitucio":
            if substitute_id is None:
                raise ValueError("La substitució no té treballador substitut")
            conflict = connection.execute(
                """
                SELECT 1 FROM descansos_dies
                WHERE CAST(treballador_id AS TEXT) = CAST(? AS TEXT)
                  AND data BETWEEN ? AND ?
                  AND COALESCE(origen, '') <> 'base'
                LIMIT 1
                """,
                (
                    substitute_id,
                    start_date.isoformat(),
                    end_date.isoformat(),
                ),
            ).fetchone()
            active_substitution = connection.execute(
                """
                SELECT 1 FROM descansos_dies
                WHERE CAST(treballador_substitut_id AS TEXT) = CAST(? AS TEXT)
                  AND data BETWEEN ? AND ?
                  AND CAST(treballador_id AS TEXT) <> CAST(? AS TEXT)
                LIMIT 1
                """,
                (
                    substitute_id,
                    start_date.isoformat(),
                    end_date.isoformat(),
                    affected_worker_id,
                ),
            ).fetchone()
            if conflict or active_substitution:
                raise ValueError(
                    "El substitut indicat té una indisponibilitat no substituïble"
                )

        if incident_type == "alta_anticipada":
            affected_need_ids = {
                f"{row['data']}::{row['servei']}"
                for row in connection.execute(
                    """
                    SELECT DISTINCT c.data, c.servei
                    FROM cobertura c
                    LEFT JOIN assig_grup_T a
                      ON a.data = c.data AND a.torn = c.servei
                     AND a.estat_planificacio IN ('publicada', 'bloquejada')
                    WHERE c.data BETWEEN ? AND ? AND a.id IS NULL
                    """,
                    (start_date.isoformat(), end_date.isoformat()),
                )
            }
        else:
            affected_need_ids = {
                f"{row['data']}::{row['torn']}"
                for row in connection.execute(
                    """
                    SELECT data, torn FROM assig_grup_T
                    WHERE CAST(treballador_id AS TEXT) = CAST(? AS TEXT)
                      AND data BETWEEN ? AND ?
                      AND estat_planificacio IN ('publicada', 'bloquejada')
                    """,
                    (
                        affected_worker_id,
                        start_date.isoformat(),
                        end_date.isoformat(),
                    ),
                )
            }

    incident_dates = {
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
    }
    unavailable = (
        frozenset()
        if incident_type == "alta_anticipada"
        else frozenset((affected_worker_id, day) for day in incident_dates)
    )
    released = {
        (affected_worker_id, day) for day in released_dates
    }
    if incident_type == "substitucio" and substitute_id is not None:
        released.update((substitute_id, day) for day in incident_dates)
    preferences = (
        tuple((need_id, substitute_id) for need_id in sorted(affected_need_ids))
        if incident_type == "substitucio" and substitute_id is not None
        else ()
    )
    request = PlanningExecutionRequest(
        scope=PlanningScope(start_date, end_date),
        trigger=PlanningTrigger(
            kind=PlanningTriggerKind.INCIDENT,
            source_id=incidence_id,
            affected_need_ids=affected_need_ids,
            reason=f"Incidència {incident_type}",
        ),
        adjustments=PlanningInputAdjustments(
            unavailable_worker_dates=unavailable,
            released_worker_dates=released,
            preferred_assignments=preferences,
            allow_active_assignments_without_coverage=True,
        ),
    )
    prepared = prepare_planning_problem(database_path, request)
    sources = tuple(
        PlanSource(
            assignment_id=item.assignment_id,
            need_id=item.need_id,
            worker_id=item.worker_id,
            state=item.state,
            date=item.date,
            turn=item.service_id,
        )
        for item in prepared.snapshot.assignments
    )
    return IncidentPlanningContext(
        incidence_id=incidence_id,
        incident_type=incident_type,
        start_date=start_date,
        end_date=end_date,
        problem=prepared.problem,
        sources=sources,
        snapshot_hash=prepared.snapshot.fingerprint,
        prepared=prepared,
    )


def _changes_from_result(
    context: IncidentPlanningContext,
    result: SolveResult,
) -> tuple[dict[str, Any], ...]:
    needs_by_id = {need.id: need for need in context.problem.needs}

    def need_details(need_id: str) -> dict[str, Any]:
        need = needs_by_id[need_id]
        return {
            "hora_inici": need.start.strftime("%H:%M"),
            "hora_fi": need.end.strftime("%H:%M"),
            "durada_hores": need.duration_minutes / 60,
            "zona": need.zone,
        }

    assigned_by_need = {
        assignment.need_id: assignment for assignment in result.assignments
    }
    changes: list[dict[str, Any]] = []
    for source in context.sources:
        proposed = assigned_by_need.get(source.need_id)
        if proposed is not None and proposed.worker_id == source.worker_id:
            continue
        changes.append(
            {
                "tipus": "assignacio_a_reemplaçar",
                "assignacio_id": source.assignment_id,
                "necessitat_id": source.need_id,
                "data": source.date.isoformat(),
                "torn": source.turn,
                "treballador_id": source.worker_id,
                "descripcio": (
                    "Assignació afectada per la incidència o reubicada "
                    "per conservar la màxima cobertura."
                ),
                **need_details(source.need_id),
            }
        )
        if proposed is None:
            changes.append(
                {
                    "tipus": "servei_sense_cobertura",
                    "assignacio_id": source.assignment_id,
                    "necessitat_id": source.need_id,
                    "data": source.date.isoformat(),
                    "torn": source.turn,
                    "treballador_id": None,
                    "descripcio": (
                        "CP-SAT no ha trobat cap assignació compatible "
                        "sense vulnerar les restriccions dures."
                    ),
                    **need_details(source.need_id),
                }
            )
        else:
            changes.append(
                {
                    "tipus": "assignacio_proposada",
                    "assignacio_id": source.assignment_id,
                    "necessitat_id": source.need_id,
                    "data": source.date.isoformat(),
                    "torn": source.turn,
                    "treballador_id": proposed.worker_id,
                    "descripcio": (
                        f"CP-SAT proposa {proposed.worker_id} per cobrir "
                        f"{source.turn} el {source.date.isoformat()}."
                    ),
                    **need_details(source.need_id),
                }
            )
    source_need_ids = {source.need_id for source in context.sources}
    for need in context.problem.needs:
        if need.id in source_need_ids:
            continue
        proposed = assigned_by_need.get(need.id)
        if proposed is None:
            changes.append(
                {
                    "tipus": "servei_sense_cobertura",
                    "assignacio_id": None,
                    "necessitat_id": need.id,
                    "data": need.date.isoformat(),
                    "torn": need.service_id,
                    "treballador_id": None,
                    "descripcio": (
                        "La necessitat continua descoberta després de "
                        "l'alta anticipada."
                    ),
                    **need_details(need.id),
                }
            )
        else:
            changes.append(
                {
                    "tipus": "assignacio_proposada",
                    "assignacio_id": None,
                    "necessitat_id": need.id,
                    "data": need.date.isoformat(),
                    "torn": need.service_id,
                    "treballador_id": proposed.worker_id,
                    "descripcio": (
                        f"CP-SAT proposa {proposed.worker_id} per recuperar "
                        f"{need.service_id} el {need.date.isoformat()}."
                    ),
                    **need_details(need.id),
                }
            )
    return tuple(changes)


def generate_incident_draft(
    database_path: str | Path,
    incidence_id: int,
    *,
    config: SolverConfig | None = None,
) -> IncidentCpSatDraft:
    context = prepare_incident_problem(database_path, incidence_id)
    solver_config = config or SolverConfig(
            max_time_seconds=15,
            equity_time_seconds=15,
            num_workers=8,
            random_seed=0,
        )
    proposal = generate_planning_proposal(
        context.prepared,
        config=solver_config,
        seeds=(solver_config.random_seed,),
        force_all_seeds=True,
    )
    result = proposal.result
    if not result.feasible:
        raise ValueError(
            "CP-SAT no ha produït cap proposta factible i validada"
        )
    return IncidentCpSatDraft(
        context=context,
        result=result,
        changes=_changes_from_result(context, result),
        proposal=proposal,
    )
