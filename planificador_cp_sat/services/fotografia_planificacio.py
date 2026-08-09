"""Fotografia canònica i de només lectura del pla operatiu vigent."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

from planificador_cp_sat.domain import PlanningScope


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PILOT_SRC = PROJECT_ROOT / "cp_sat_pilot" / "src"
if str(PILOT_SRC) not in sys.path:
    sys.path.insert(0, str(PILOT_SRC))

from cp_sat_pilot import Assignment, Need  # noqa: E402
from cp_sat_pilot.sqlite_adapter import (  # noqa: E402
    SqliteInputError,
    load_problem_from_sqlite,
)


BOUNDARY_DAYS = 2
ACTIVE_STATES = frozenset({"publicada", "bloquejada"})


class PlanningSnapshotError(ValueError):
    """Indica que el pla operatiu no admet una fotografia inequívoca."""


@dataclass(frozen=True, slots=True)
class CoverageNeedSnapshot:
    """Necessitat convertida al domini CP-SAT i el seu context operatiu."""

    need: Need
    line: str
    in_scope: bool


@dataclass(frozen=True, slots=True)
class ActiveAssignmentSnapshot:
    """Assignació activa observada sense modificar-ne la fila d'origen."""

    assignment_id: int
    need_id: str
    worker_id: str
    state: str
    date: date
    service_id: str
    start: datetime
    end: datetime
    duration_minutes: int
    line: str
    zone: str
    required_skills: frozenset[str]
    zone_change: bool
    turn_change: bool
    created_at: str | None
    in_scope: bool
    is_boundary: bool

    def as_assignment(self) -> Assignment:
        """Converteix la fila publicada en una assignació de referència."""
        return Assignment(
            worker_id=self.worker_id,
            need_id=self.need_id,
            service_id=self.service_id,
            date=self.date,
            start=self.start,
            end=self.end,
            duration_minutes=self.duration_minutes,
        )


@dataclass(frozen=True, slots=True)
class PlanningSnapshot:
    """Estat immutable utilitzat per generar i revalidar una proposta."""

    scope: PlanningScope
    needs: tuple[CoverageNeedSnapshot, ...]
    assignments: tuple[ActiveAssignmentSnapshot, ...]
    boundary_assignments: tuple[ActiveAssignmentSnapshot, ...]
    captured_start: date
    captured_end: date
    fingerprint: str

    @property
    def all_assignments(self) -> tuple[ActiveAssignmentSnapshot, ...]:
        return self.assignments + self.boundary_assignments


def _readonly_connection(database_path: str | Path) -> sqlite3.Connection:
    resolved = Path(database_path).resolve()
    encoded = quote(resolved.as_posix(), safe="/:")
    connection = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _split_values(raw_value: object) -> frozenset[str]:
    return frozenset(
        part.strip().upper()
        for part in str(raw_value or "")
        .replace("+", ",")
        .replace(";", ",")
        .split(",")
        if part.strip()
    )


def _assignment_interval(row: sqlite3.Row) -> tuple[datetime, datetime]:
    try:
        start = datetime.fromisoformat(f"{row['data']}T{row['hora_inici']}")
        end = datetime.fromisoformat(f"{row['data']}T{row['hora_fi']}")
    except (TypeError, ValueError) as error:
        raise PlanningSnapshotError(
            f"L'assignació activa #{row['id']} té un horari invàlid"
        ) from error
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _assignment_duration(row: sqlite3.Row, start: datetime, end: datetime) -> int:
    try:
        duration = round(float(row["durada_hores"]) * 60)
    except (TypeError, ValueError) as error:
        raise PlanningSnapshotError(
            f"L'assignació activa #{row['id']} té una durada invàlida"
        ) from error
    interval_duration = int((end - start).total_seconds() // 60)
    if duration <= 0 or duration != interval_duration:
        raise PlanningSnapshotError(
            f"L'assignació activa #{row['id']} té una durada incoherent "
            "amb el seu horari"
        )
    return duration


def _need_is_in_scope(need: Need, scope: PlanningScope) -> bool:
    return not scope.service_ids or need.service_id in scope.service_ids


def _assignment_is_in_scope(
    assignment_id: int,
    worker_id: str,
    service_id: str,
    scope: PlanningScope,
) -> bool:
    return (
        (not scope.worker_ids or worker_id in scope.worker_ids)
        and (not scope.service_ids or service_id in scope.service_ids)
        and (
            not scope.assignment_ids
            or assignment_id in scope.assignment_ids
        )
    )


def planning_snapshot_hash(
    scope: PlanningScope,
    needs: Iterable[CoverageNeedSnapshot],
    assignments: Iterable[ActiveAssignmentSnapshot],
) -> str:
    """Calcula una empremta estable independent de l'ordre físic de SQLite."""
    payload = {
        "scope": {
            "start_date": scope.start_date.isoformat(),
            "end_date": scope.end_date.isoformat(),
            "worker_ids": sorted(scope.worker_ids),
            "service_ids": sorted(scope.service_ids),
            "assignment_ids": sorted(scope.assignment_ids),
        },
        "needs": [
            {
                "id": item.need.id,
                "service_id": item.need.service_id,
                "date": item.need.date.isoformat(),
                "start": item.need.start.isoformat(),
                "end": item.need.end.isoformat(),
                "duration_minutes": item.need.duration_minutes,
                "required_skills": sorted(item.need.required_skills),
                "zone": item.need.zone,
                "turn_options": sorted(item.need.turn_options),
                "line": item.line,
                "in_scope": item.in_scope,
            }
            for item in sorted(
                needs,
                key=lambda current: (
                    current.need.date,
                    current.need.service_id,
                    current.need.id,
                ),
            )
        ],
        "assignments": [
            {
                "assignment_id": item.assignment_id,
                "need_id": item.need_id,
                "worker_id": item.worker_id,
                "state": item.state,
                "date": item.date.isoformat(),
                "service_id": item.service_id,
                "start": item.start.isoformat(),
                "end": item.end.isoformat(),
                "duration_minutes": item.duration_minutes,
                "line": item.line,
                "zone": item.zone,
                "required_skills": sorted(item.required_skills),
                "zone_change": item.zone_change,
                "turn_change": item.turn_change,
                "created_at": item.created_at,
                "in_scope": item.in_scope,
                "is_boundary": item.is_boundary,
            }
            for item in sorted(
                assignments,
                key=lambda current: (
                    current.date,
                    current.service_id,
                    current.assignment_id,
                ),
            )
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_planning_snapshot(
    database_path: str | Path,
    scope: PlanningScope,
    *,
    boundary_days: int = BOUNDARY_DAYS,
    allow_active_assignments_without_coverage: bool = False,
) -> PlanningSnapshot:
    """Llegeix necessitats i pla actiu sense obrir cap escriptura SQLite."""
    if not isinstance(scope, PlanningScope):
        raise TypeError("scope ha de ser PlanningScope")
    if (
        not isinstance(boundary_days, int)
        or isinstance(boundary_days, bool)
        or boundary_days < 0
    ):
        raise ValueError("boundary_days ha de ser un enter no negatiu")
    if not isinstance(allow_active_assignments_without_coverage, bool):
        raise TypeError(
            "allow_active_assignments_without_coverage ha de ser booleà"
        )

    try:
        base_problem = load_problem_from_sqlite(
            database_path,
            start_date=scope.start_date,
            end_date=scope.end_date,
            duplicate_policy="replace_all",
            allow_empty_needs=True,
        )
    except SqliteInputError as error:
        raise PlanningSnapshotError(str(error)) from error

    captured_start = scope.start_date - timedelta(days=boundary_days)
    captured_end = scope.end_date + timedelta(days=boundary_days)
    worker_ids = {worker.id for worker in base_problem.workers}

    with closing(_readonly_connection(database_path)) as connection:
        coverage_rows = connection.execute(
            """
            SELECT data, servei, linia
            FROM cobertura
            WHERE data BETWEEN ? AND ?
            ORDER BY data, servei
            """,
            (scope.start_date.isoformat(), scope.end_date.isoformat()),
        ).fetchall()
        active_rows = connection.execute(
            """
            SELECT id, data, torn, treballador_id, hora_inici, hora_fi,
                   durada_hores, linia, zona, formacio,
                   es_canvi_zona, es_canvi_torn,
                   estat_planificacio, created_at
            FROM assig_grup_T
            WHERE data BETWEEN ? AND ?
              AND estat_planificacio IN ('publicada', 'bloquejada')
            ORDER BY data, torn, id
            """,
            (captured_start.isoformat(), captured_end.isoformat()),
        ).fetchall()

    coverage_by_need = {
        f"{row['data']}::{row['servei']}": row for row in coverage_rows
    }
    needs = list(
        CoverageNeedSnapshot(
            need=need,
            line=str(coverage_by_need[need.id]["linia"] or ""),
            in_scope=_need_is_in_scope(need, scope),
        )
        for need in sorted(
            base_problem.needs,
            key=lambda current: (
                current.date,
                current.service_id,
                current.id,
            ),
        )
    )
    need_ids = {item.need.id for item in needs}

    if allow_active_assignments_without_coverage:
        for row in active_rows:
            day = date.fromisoformat(row["data"])
            if not (scope.start_date <= day <= scope.end_date):
                continue
            service_id = str(row["torn"])
            need_id = f"{day.isoformat()}::{service_id}"
            if need_id in need_ids:
                continue
            start, end = _assignment_interval(row)
            duration = _assignment_duration(row, start, end)
            needs.append(
                CoverageNeedSnapshot(
                    need=Need(
                        id=need_id,
                        service_id=service_id,
                        date=day,
                        start=start,
                        end=end,
                        required_skills=_split_values(row["formacio"]),
                        zone=str(row["zona"] or ""),
                        turn_options=frozenset(),
                    ),
                    line=str(row["linia"] or ""),
                    in_scope=_need_is_in_scope(
                        Need(
                            id=need_id,
                            service_id=service_id,
                            date=day,
                            start=start,
                            end=end,
                            required_skills=_split_values(row["formacio"]),
                            zone=str(row["zona"] or ""),
                            turn_options=frozenset(),
                        ),
                        scope,
                    ),
                )
            )
            need_ids.add(need_id)
        needs.sort(
            key=lambda current: (
                current.need.date,
                current.need.service_id,
                current.need.id,
            )
        )

    rows_by_need: dict[str, list[sqlite3.Row]] = {}
    for row in active_rows:
        need_id = f"{row['data']}::{row['torn']}"
        rows_by_need.setdefault(need_id, []).append(row)
    duplicate = next(
        (
            (need_id, rows)
            for need_id, rows in rows_by_need.items()
            if len(rows) > 1
        ),
        None,
    )
    if duplicate is not None:
        need_id, rows = duplicate
        identifiers = ", ".join(f"#{row['id']}" for row in rows)
        raise PlanningSnapshotError(
            "El pla actiu conté més d'una assignació per necessitat: "
            f"{need_id} ({identifiers})"
        )

    assignments: list[ActiveAssignmentSnapshot] = []
    boundary_assignments: list[ActiveAssignmentSnapshot] = []
    for row in active_rows:
        assignment_id = int(row["id"])
        day = date.fromisoformat(row["data"])
        service_id = str(row["torn"])
        need_id = f"{day.isoformat()}::{service_id}"
        is_boundary = not (scope.start_date <= day <= scope.end_date)
        if not is_boundary and need_id not in need_ids:
            raise PlanningSnapshotError(
                f"L'assignació activa #{assignment_id} ({need_id}) no té "
                "una necessitat equivalent a cobertura dins del període"
            )

        worker_id = str(row["treballador_id"])
        if worker_id not in worker_ids:
            raise PlanningSnapshotError(
                f"L'assignació activa #{assignment_id} referencia el "
                f"treballador inexistent {worker_id}"
            )
        start, end = _assignment_interval(row)
        duration = _assignment_duration(row, start, end)
        item = ActiveAssignmentSnapshot(
            assignment_id=assignment_id,
            need_id=need_id,
            worker_id=worker_id,
            state=str(row["estat_planificacio"]),
            date=day,
            service_id=service_id,
            start=start,
            end=end,
            duration_minutes=duration,
            line=str(row["linia"] or ""),
            zone=str(row["zona"] or ""),
            required_skills=_split_values(row["formacio"]),
            zone_change=bool(row["es_canvi_zona"]),
            turn_change=bool(row["es_canvi_torn"]),
            created_at=(
                str(row["created_at"]) if row["created_at"] else None
            ),
            in_scope=(
                False
                if is_boundary
                else _assignment_is_in_scope(
                    assignment_id,
                    worker_id,
                    service_id,
                    scope,
                )
            ),
            is_boundary=is_boundary,
        )
        if is_boundary:
            boundary_assignments.append(item)
        else:
            assignments.append(item)

    ordered_assignments = tuple(assignments)
    ordered_boundary = tuple(boundary_assignments)
    fingerprint = planning_snapshot_hash(
        scope,
        needs,
        ordered_assignments + ordered_boundary,
    )
    return PlanningSnapshot(
        scope=scope,
        needs=tuple(needs),
        assignments=ordered_assignments,
        boundary_assignments=ordered_boundary,
        captured_start=captured_start,
        captured_end=captured_end,
        fingerprint=fingerprint,
    )
