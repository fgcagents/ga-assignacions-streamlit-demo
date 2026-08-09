"""Adaptador SQLite nadiu per al domini CP-SAT.

La càrrega és estrictament de només lectura i no depèn de cap mòdul del
sistema GA retirat.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote

from .domain import HistoricalAssignment, Need, PlanningProblem, Worker


class SqliteInputError(ValueError):
    """Indica que l'esquema o les dades d'entrada no són planificables."""


@dataclass(frozen=True, slots=True)
class _ServiceWindow:
    day_codes: frozenset[str]
    start: time
    end: time


@dataclass(frozen=True, slots=True)
class _CoverageNeed:
    service_id: str
    date: date
    skills: frozenset[str]
    zone: str
    turn: str


def _readonly_connection(database_path: str | Path) -> sqlite3.Connection:
    resolved = Path(database_path).resolve()
    encoded = quote(resolved.as_posix(), safe="/:")
    connection = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _parse_time(raw_value: object) -> time:
    value = str(raw_value or "").replace('"', "").strip()
    if not value:
        raise ValueError("Hora buida")
    try:
        if ":" in value:
            parts = value.split(":")
            if len(parts) not in {2, 3}:
                raise ValueError
            hours = int(parts[0])
            minutes = int(parts[1])
        else:
            digits = "".join(character for character in value if character.isdigit())
            if not digits:
                raise ValueError
            if len(digits) <= 2:
                hours, minutes = int(digits), 0
            elif len(digits) == 3:
                hours, minutes = int(digits[0]), int(digits[1:])
            else:
                hours, minutes = int(digits[:-2]), int(digits[-2:])
        return time(hour=hours % 24, minute=minutes)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Format d'hora invàlid: {raw_value}") from error


def _parse_date(raw_value: object) -> date:
    value = str(raw_value or "").strip()
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"Format de data no reconegut: {raw_value}")


def _split_values(raw_value: object) -> frozenset[str]:
    value = str(raw_value or "").replace("+", ",")
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def _turn_options(raw_value: object) -> frozenset[str]:
    return frozenset(
        part.strip().lower().replace("í", "i").replace("\u00a0", " ")
        for part in str(raw_value or "").split(",")
        if part.strip()
    )


def _service_windows(
    connection: sqlite3.Connection,
) -> dict[str, tuple[_ServiceWindow, ...]]:
    windows: dict[str, tuple[_ServiceWindow, ...]] = {}
    for row in connection.execute('SELECT * FROM "serveis_horaris"'):
        service_id = str(row["Torn"])
        variants: list[_ServiceWindow] = []
        for index in range(1, 5):
            codes = row[f"Servei {index}"]
            start = row[f"Inici S{index}"]
            end = row[f"Final S{index}"]
            if not (codes and start and end):
                continue
            day_codes = frozenset(
                part.strip()
                for part in str(codes).replace('"', "").split(",")
                if part.strip()
            )
            variants.append(
                _ServiceWindow(
                    day_codes=day_codes,
                    start=_parse_time(start),
                    end=_parse_time(end),
                )
            )
        windows[service_id] = tuple(variants)
    return windows


def _calendar(connection: sqlite3.Connection) -> dict[date, str]:
    return {
        _parse_date(row["Data"]): str(row["Servei BV"] or "").strip()
        for row in connection.execute('SELECT "Data", "Servei BV" FROM "serveis_calendari"')
    }


def _coverage_needs(connection: sqlite3.Connection) -> list[_CoverageNeed]:
    needs: list[_CoverageNeed] = []
    for row in connection.execute('SELECT * FROM "cobertura"'):
        needs.append(
            _CoverageNeed(
                service_id=str(row["servei"] or ""),
                date=_parse_date(row["data"]),
                skills=_split_values(row["formacio"]),
                zone=str(row["zona"] or ""),
                turn=str(row["rotacio"] or row["torn"] or ""),
            )
        )
    return needs


def load_problem_from_sqlite(
    database_path: str | Path,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    duplicate_policy: str = "replace_all",
    allow_empty_needs: bool = False,
) -> PlanningProblem:
    """Converteix les taules operatives SQLite al domini CP-SAT."""
    if duplicate_policy not in {"replace_all", "add_new_only"}:
        raise SqliteInputError(
            "duplicate_policy ha de ser 'replace_all' o 'add_new_only'"
        )
    if start_date and end_date and start_date > end_date:
        start_date, end_date = end_date, start_date

    try:
        connection = _readonly_connection(database_path)
    except sqlite3.Error as error:
        raise SqliteInputError("No s'ha pogut obrir la base de dades") from error

    try:
        windows = _service_windows(connection)
        calendar = _calendar(connection)
        coverage = _coverage_needs(connection)
        if start_date:
            coverage = [item for item in coverage if item.date >= start_date]
        if end_date:
            coverage = [item for item in coverage if item.date <= end_date]
        if not coverage and not allow_empty_needs:
            raise SqliteInputError(
                "No hi ha necessitats dins de l'interval seleccionat"
            )

        rest_dates: dict[str, set[date]] = {}
        for row in connection.execute(
            'SELECT "treballador_id", "data" FROM "descansos_dies"'
        ):
            worker_id = str(row["treballador_id"])
            rest_dates.setdefault(worker_id, set()).add(_parse_date(row["data"]))

        worker_rows = list(connection.execute('SELECT * FROM "treballadors"'))
        worker_ids = {str(row["id"]) for row in worker_rows}
        need_dates = {item.date for item in coverage}
        annual_minutes: dict[str, int] = {}
        removed_minutes: dict[str, int] = {}
        historical_counts: dict[str, int] = {}
        historical_zone_changes: dict[str, int] = {}
        historical_turn_changes: dict[str, int] = {}
        exclusions: set[tuple[str, date]] = set()
        history_by_worker: dict[str, list[HistoricalAssignment]] = {}

        for row in connection.execute('SELECT * FROM "historic_assignacions"'):
            worker_id = str(row["treballador_id"])
            if worker_id not in worker_ids:
                continue
            assignment_date = _parse_date(row["data"])
            duration_minutes = round(float(row["durada_hores"] or 0) * 60)
            annual_minutes[worker_id] = (
                annual_minutes.get(worker_id, 0) + duration_minutes
            )
            overlaps_horizon = assignment_date in need_dates
            if duplicate_policy == "replace_all" and overlaps_horizon:
                removed_minutes[worker_id] = (
                    removed_minutes.get(worker_id, 0) + duration_minutes
                )
                continue
            if duplicate_policy == "add_new_only" and overlaps_horizon:
                exclusions.add((worker_id, assignment_date))

            zone_change = bool(row["es_canvi_zona"])
            turn_change = bool(row["es_canvi_torn"])
            historical_counts[worker_id] = historical_counts.get(worker_id, 0) + 1
            historical_zone_changes[worker_id] = (
                historical_zone_changes.get(worker_id, 0) + int(zone_change)
            )
            historical_turn_changes[worker_id] = (
                historical_turn_changes.get(worker_id, 0) + int(turn_change)
            )
            start_time = _parse_time(row["hora_inici"])
            end_time = _parse_time(row["hora_fi"])
            start = datetime.combine(assignment_date, start_time)
            end = datetime.combine(assignment_date, end_time)
            if end_time < start_time:
                end += timedelta(days=1)
            history_by_worker.setdefault(worker_id, []).append(
                HistoricalAssignment(
                    worker_id=worker_id,
                    start=start,
                    end=end,
                    duration_minutes=duration_minutes,
                    zone_change=zone_change,
                    turn_change=turn_change,
                )
            )

        workers = tuple(
            Worker(
                id=str(row["id"]),
                group=str(row["grup"] or ""),
                skills=_split_values(row["habilitacions"]),
                rest_dates=frozenset(rest_dates.get(str(row["id"]), set())),
                annual_minutes=max(
                    0,
                    annual_minutes.get(str(row["id"]), 0)
                    - removed_minutes.get(str(row["id"]), 0),
                ),
                max_annual_minutes=1605 * 60,
                home_zone=str(row["zona"] or ""),
                turn_options=_turn_options(row["rotacio"]),
                historical_assignments=historical_counts.get(str(row["id"]), 0),
                historical_zone_changes=historical_zone_changes.get(
                    str(row["id"]), 0
                ),
                historical_turn_changes=historical_turn_changes.get(
                    str(row["id"]), 0
                ),
            )
            for row in worker_rows
        )

        converted_needs: list[Need] = []
        seen_need_ids: set[str] = set()
        input_errors: list[str] = []
        for item in coverage:
            service_windows = windows.get(item.service_id)
            if service_windows is None:
                input_errors.append(
                    f"Torn inexistent: {item.service_id} / {item.date}"
                )
                continue
            day_code = calendar.get(item.date)
            if day_code is None:
                input_errors.append(f"Data {item.date} no trobada al calendari")
                continue
            window = next(
                (
                    candidate
                    for candidate in service_windows
                    if day_code in candidate.day_codes
                ),
                None,
            )
            if window is None:
                input_errors.append(
                    f"Codi servei {day_code} no trobat al torn {item.service_id}"
                )
                continue
            need_id = f"{item.date.isoformat()}::{item.service_id}"
            if need_id in seen_need_ids:
                input_errors.append(f"Necessitat duplicada: {need_id}")
                continue
            seen_need_ids.add(need_id)
            start = datetime.combine(item.date, window.start)
            end = datetime.combine(item.date, window.end)
            if window.end < window.start:
                end += timedelta(days=1)
            converted_needs.append(
                Need(
                    id=need_id,
                    service_id=item.service_id,
                    date=item.date,
                    start=start,
                    end=end,
                    required_skills=item.skills,
                    zone=item.zone,
                    turn_options=_turn_options(item.turn),
                )
            )

        if input_errors:
            preview = "\n".join(f"- {item}" for item in input_errors[:20])
            suffix = (
                f"\n- ... i {len(input_errors) - 20} errors més"
                if len(input_errors) > 20
                else ""
            )
            raise SqliteInputError(
                f"Errors de dades d'entrada:\n{preview}{suffix}"
            )

        history = tuple(
            assignment
            for worker_history in history_by_worker.values()
            for assignment in worker_history
        )
        return PlanningProblem(
            workers=workers,
            needs=tuple(converted_needs),
            history=history,
            exclusions=frozenset(exclusions),
        )
    except sqlite3.Error as error:
        raise SqliteInputError(f"Error llegint la base de dades: {error}") from error
    finally:
        connection.close()
