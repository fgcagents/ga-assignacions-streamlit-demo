"""Generació i persistència separada de propostes inicials CP-SAT."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from contextlib import closing, redirect_stdout
from dataclasses import asdict
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable
from urllib.parse import quote


PILOT_SRC = Path(__file__).resolve().parent / "cp_sat_pilot" / "src"
if str(PILOT_SRC) not in sys.path:
    sys.path.insert(0, str(PILOT_SRC))

if TYPE_CHECKING:
    from cp_sat_pilot import PlanningProblem, SolverConfig


def _readonly_connection(database_path: str | Path) -> sqlite3.Connection:
    resolved = Path(database_path).resolve()
    encoded = quote(resolved.as_posix(), safe="/:")
    connection = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _write_connection(database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database_path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_cp_sat_drafts(database_path: str | Path) -> None:
    """Crea l'espai d'esborranys sense tocar el pla operatiu."""
    with closing(_write_connection(database_path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS propostes_inicials_cp_sat (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                estat TEXT NOT NULL DEFAULT 'esborrany'
                    CHECK (estat IN (
                        'esborrany', 'validada', 'descartada', 'publicada'
                    )),
                data_inici TEXT NOT NULL,
                data_fi TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                solver_status TEXT NOT NULL,
                necessitats_cobertes INTEGER NOT NULL,
                necessitats_totals INTEGER NOT NULL,
                temps_total_segons REAL NOT NULL,
                llavor_seleccionada INTEGER NOT NULL,
                aturada_primera_llavor INTEGER NOT NULL DEFAULT 0,
                configuracio_json TEXT NOT NULL,
                llavors_json TEXT NOT NULL,
                snapshot_hash TEXT NOT NULL,
                fases_json TEXT NOT NULL,
                metriques_json TEXT,
                validacio_funcional_json TEXT,
                candidats_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS proposta_inicial_cp_sat_elements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposta_id INTEGER NOT NULL,
                tipus TEXT NOT NULL
                    CHECK (tipus IN ('assignacio', 'descoberta')),
                necessitat_id TEXT NOT NULL,
                data TEXT NOT NULL,
                servei TEXT NOT NULL,
                treballador_id TEXT,
                treballador TEXT,
                hora_inici TEXT NOT NULL,
                hora_fi TEXT NOT NULL,
                durada_hores REAL NOT NULL,
                zona TEXT,
                torn_requerit TEXT,
                habilitacions TEXT,
                motiu_descobert TEXT,
                diagnostic_json TEXT,
                FOREIGN KEY (proposta_id)
                    REFERENCES propostes_inicials_cp_sat(id)
                    ON DELETE CASCADE,
                UNIQUE (proposta_id, necessitat_id)
            );

            CREATE INDEX IF NOT EXISTS idx_propostes_inicials_cp_sat_periode
                ON propostes_inicials_cp_sat(data_inici, data_fi, estat);
            CREATE INDEX IF NOT EXISTS idx_elements_cp_sat_proposta
                ON proposta_inicial_cp_sat_elements(proposta_id, tipus, data);

            CREATE TABLE IF NOT EXISTS publicacions_inicials_cp_sat (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposta_id INTEGER NOT NULL,
                estat TEXT NOT NULL DEFAULT 'publicada'
                    CHECK (estat IN ('publicada', 'revertida')),
                snapshot_hash TEXT NOT NULL,
                operational_snapshot_before TEXT NOT NULL,
                operational_snapshot_after TEXT NOT NULL,
                backup_path TEXT NOT NULL,
                assignacions_publicades INTEGER NOT NULL,
                assignacions_previes_anullades INTEGER NOT NULL,
                rollback_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                rolled_back_at TEXT,
                FOREIGN KEY (proposta_id)
                    REFERENCES propostes_inicials_cp_sat(id)
            );

            CREATE INDEX IF NOT EXISTS idx_publicacions_cp_sat_proposta
                ON publicacions_inicials_cp_sat(proposta_id, id);
            """
        )
        header_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(propostes_inicials_cp_sat)"
            )
        }
        if "validacio_funcional_json" not in header_columns:
            connection.execute(
                """
                ALTER TABLE propostes_inicials_cp_sat
                ADD COLUMN validacio_funcional_json TEXT
                """
            )
        if "operational_snapshot_hash" not in header_columns:
            connection.execute(
                """
                ALTER TABLE propostes_inicials_cp_sat
                ADD COLUMN operational_snapshot_hash TEXT
                """
            )
        element_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(proposta_inicial_cp_sat_elements)"
            )
        }
        if "torn_requerit" not in element_columns:
            connection.execute(
                """
                ALTER TABLE proposta_inicial_cp_sat_elements
                ADD COLUMN torn_requerit TEXT
                """
            )
        if "motiu_descobert" not in element_columns:
            connection.execute(
                """
                ALTER TABLE proposta_inicial_cp_sat_elements
                ADD COLUMN motiu_descobert TEXT
                """
            )
        if "diagnostic_json" not in element_columns:
            connection.execute(
                """
                ALTER TABLE proposta_inicial_cp_sat_elements
                ADD COLUMN diagnostic_json TEXT
                """
            )


def _problem_snapshot_hash(problem: PlanningProblem) -> str:
    payload = {
        "workers": [
            {
                "id": worker.id,
                "group": worker.group,
                "skills": sorted(worker.skills),
                "rests": sorted(day.isoformat() for day in worker.rest_dates),
                "annual": worker.annual_minutes,
                "maximum": worker.max_annual_minutes,
                "zone": worker.home_zone,
                "turns": sorted(worker.turn_options),
                "assignments": worker.historical_assignments,
                "zone_changes": worker.historical_zone_changes,
                "turn_changes": worker.historical_turn_changes,
            }
            for worker in sorted(problem.workers, key=lambda item: item.id)
        ],
        "needs": [
            {
                "id": need.id,
                "service": need.service_id,
                "start": need.start.isoformat(),
                "end": need.end.isoformat(),
                "skills": sorted(need.required_skills),
                "zone": need.zone,
                "turns": sorted(need.turn_options),
            }
            for need in sorted(problem.needs, key=lambda item: item.id)
        ],
        "history": [
            {
                "worker": item.worker_id,
                "start": item.start.isoformat(),
                "end": item.end.isoformat(),
                "minutes": item.duration_minutes,
                "zone_change": item.zone_change,
                "turn_change": item.turn_change,
            }
            for item in sorted(
                problem.history,
                key=lambda value: (value.worker_id, value.start, value.end),
            )
        ],
        "exclusions": sorted(
            (worker_id, day.isoformat())
            for worker_id, day in problem.exclusions
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rows_as_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in rows]


def _operational_snapshot_hash_connection(
    connection: sqlite3.Connection,
    start_date: date,
    end_date: date,
) -> str:
    """Empremta del pla i l'històric que una publicació substituiria."""
    assignments = _rows_as_dicts(
        connection.execute(
            """
            SELECT * FROM assig_grup_T
            WHERE data BETWEEN ? AND ?
            ORDER BY id
            """,
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
    )
    history = _rows_as_dicts(
        connection.execute(
            """
            SELECT rowid AS _rowid, * FROM historic_assignacions
            WHERE data BETWEEN ? AND ?
            ORDER BY rowid
            """,
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
    )
    encoded = json.dumps(
        {"assignments": assignments, "history": history},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _operational_snapshot_hash(
    database_path: str | Path,
    start_date: date,
    end_date: date,
) -> str:
    with closing(_readonly_connection(database_path)) as connection:
        return _operational_snapshot_hash_connection(
            connection, start_date, end_date
        )


def _create_sqlite_backup(
    source_database: str | Path,
    draft_id: int,
    backup_directory: str | Path | None,
) -> Path:
    database = Path(source_database).resolve()
    directory = (
        Path(backup_directory).resolve()
        if backup_directory is not None
        else database.parent / "backups" / "cp_sat"
    )
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    destination = directory / (
        f"{database.stem}_abans_publicacio_cp_sat_"
        f"{draft_id}_{timestamp}{database.suffix or '.db'}"
    )
    with (
        closing(_readonly_connection(database)) as source,
        closing(sqlite3.connect(destination)) as target,
    ):
        source.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise sqlite3.DatabaseError(
                f"La còpia de seguretat no és íntegra: {integrity}"
            )
    return destination


def limits_cobertura(database_path: str | Path) -> tuple[date, date]:
    """Retorna l'interval disponible sense modificar la base de dades."""
    with closing(_readonly_connection(database_path)) as connection:
        row = connection.execute(
            "SELECT MIN(data) AS inici, MAX(data) AS fi FROM cobertura"
        ).fetchone()
    if row is None or not row["inici"] or not row["fi"]:
        raise ValueError("No hi ha necessitats de cobertura a la base de dades")
    return date.fromisoformat(row["inici"]), date.fromisoformat(row["fi"])


def _worker_names(database_path: str | Path) -> dict[str, str]:
    with closing(_readonly_connection(database_path)) as connection:
        return {
            str(row["id"]): str(row["treballador"])
            for row in connection.execute(
                "SELECT id, treballador FROM treballadors"
            )
        }


def generate_initial_coverage(
    database_path: str | Path,
    start_date: date,
    end_date: date,
    *,
    config: SolverConfig | None = None,
    seeds: Iterable[int] = (0, 1, 2),
    force_all_seeds: bool = False,
) -> dict[str, Any]:
    """Genera una cobertura completa en memòria i no publica cap resultat."""
    from cp_sat_pilot import CpSatPlanner, SolverConfig
    from cp_sat_pilot.sqlite_adapter import (
        SqliteInputError,
        load_problem_from_sqlite,
    )
    from cp_sat_pilot.multistart import solve_adaptive_multi_start

    if start_date > end_date:
        start_date, end_date = end_date, start_date
    try:
        with redirect_stdout(StringIO()):
            problem = load_problem_from_sqlite(
                database_path,
                start_date=start_date,
                end_date=end_date,
                duplicate_policy="replace_all",
            )
    except SqliteInputError as exc:
        raise ValueError(str(exc)) from exc

    solver_config = config or SolverConfig(
        max_time_seconds=60,
        equity_time_seconds=15,
        num_workers=8,
        random_seed=0,
    )
    requested_seeds = tuple(dict.fromkeys(int(seed) for seed in seeds))
    selection = solve_adaptive_multi_start(
        CpSatPlanner(problem),
        solver_config,
        requested_seeds,
        force_all_seeds=force_all_seeds,
    )
    result = selection.selected_result
    if not result.feasible:
        raise ValueError(
            "CP-SAT no ha produït cap cobertura factible i validada"
        )

    names = _worker_names(database_path)
    from cp_sat_pilot.functional_validation import analyze_functional_result

    functional_validation = analyze_functional_result(
        problem,
        result,
        worker_names=names,
    )
    uncovered_diagnostics = {
        item["necessitat_id"]: item
        for item in functional_validation["diagnostic_descobertes"]
    }
    needs = {need.id: need for need in problem.needs}
    assignments = []
    for assignment in sorted(
        result.assignments,
        key=lambda item: (item.date, item.service_id, item.worker_id),
    ):
        need = needs[assignment.need_id]
        assignments.append(
            {
                "data": assignment.date.isoformat(),
                "servei": assignment.service_id,
                "necessitat_id": assignment.need_id,
                "treballador_id": assignment.worker_id,
                "treballador": names.get(
                    assignment.worker_id, assignment.worker_id
                ),
                "hora_inici": assignment.start.strftime("%H:%M"),
                "hora_fi": assignment.end.strftime("%H:%M"),
                "durada_hores": assignment.duration_minutes / 60,
                "zona": need.zone,
                "torn_requerit": ", ".join(sorted(need.turn_options)),
                "habilitacions": ", ".join(sorted(need.required_skills)),
            }
        )

    assigned_need_ids = {
        assignment.need_id for assignment in result.assignments
    }
    uncovered = []
    for need in problem.needs:
        if need.id in assigned_need_ids:
            continue
        diagnostic = uncovered_diagnostics.get(need.id)
        uncovered.append(
            {
                "data": need.date.isoformat(),
                "servei": need.service_id,
                "necessitat_id": need.id,
                "hora_inici": need.start.strftime("%H:%M"),
                "hora_fi": need.end.strftime("%H:%M"),
                "durada_hores": need.duration_minutes / 60,
                "zona": need.zone,
                "torn_requerit": ", ".join(sorted(need.turn_options)),
                "habilitacions": ", ".join(
                    sorted(need.required_skills)
                ),
                "motiu_descobert": (
                    diagnostic["motiu"] if diagnostic else "No determinat"
                ),
                "candidats_estatics": (
                    diagnostic["candidats_estatics"]
                    if diagnostic
                    else None
                ),
                "candidats_lliures": (
                    diagnostic["compatibles_amb_proposta"]
                    if diagnostic
                    else None
                ),
                "diagnostic_descobert": diagnostic,
            }
        )

    return {
        "data_inici": start_date.isoformat(),
        "data_fi": end_date.isoformat(),
        "estat": result.status,
        "factible": result.feasible,
        "necessitats_cobertes": result.covered_needs,
        "necessitats_totals": result.total_needs,
        "temps_total_segons": selection.total_wall_time_seconds,
        "llavor_seleccionada": selection.selected_seed,
        "aturada_primera_llavor": selection.stopped_after_first_seed,
        "configuracio": asdict(solver_config),
        "llavors_demanades": list(requested_seeds),
        "snapshot_hash": _problem_snapshot_hash(problem),
        "assignacions": assignments,
        "descobertes": uncovered,
        "fases": [asdict(phase) for phase in result.optimization_phases],
        "metriques_toves": (
            asdict(result.soft_metrics) if result.soft_metrics else None
        ),
        "validacio_funcional": functional_validation,
        "candidats_multillavor": [
            asdict(candidate) for candidate in selection.candidates
        ],
        "publicada": False,
    }


def save_initial_coverage_draft(
    database_path: str | Path,
    result: dict[str, Any],
) -> int:
    """Desa una proposta en taules pròpies i retorna el seu identificador."""
    required = {
        "data_inici",
        "data_fi",
        "estat",
        "necessitats_cobertes",
        "necessitats_totals",
        "temps_total_segons",
        "llavor_seleccionada",
        "configuracio",
        "llavors_demanades",
        "snapshot_hash",
        "assignacions",
        "descobertes",
        "fases",
        "candidats_multillavor",
    }
    missing = sorted(required - set(result))
    if missing:
        raise ValueError(
            "La proposta no conté totes les dades necessàries: "
            + ", ".join(missing)
        )
    if len(result["assignacions"]) != int(result["necessitats_cobertes"]):
        raise ValueError("El recompte de cobertura no coincideix amb la proposta")
    if (
        len(result["assignacions"]) + len(result["descobertes"])
        != int(result["necessitats_totals"])
    ):
        raise ValueError("La proposta no conté totes les necessitats")

    initialize_cp_sat_drafts(database_path)
    with closing(_write_connection(database_path)) as connection, connection:
        draft_id = int(
            connection.execute(
                """
                INSERT INTO propostes_inicials_cp_sat
                (data_inici, data_fi, solver_status, necessitats_cobertes,
                 necessitats_totals, temps_total_segons, llavor_seleccionada,
                 aturada_primera_llavor, configuracio_json, llavors_json,
                 snapshot_hash, fases_json, metriques_json,
                 validacio_funcional_json, candidats_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result["data_inici"],
                    result["data_fi"],
                    result["estat"],
                    result["necessitats_cobertes"],
                    result["necessitats_totals"],
                    result["temps_total_segons"],
                    result["llavor_seleccionada"],
                    int(bool(result.get("aturada_primera_llavor"))),
                    json.dumps(result["configuracio"], ensure_ascii=False),
                    json.dumps(result["llavors_demanades"]),
                    result["snapshot_hash"],
                    json.dumps(result["fases"], ensure_ascii=False),
                    json.dumps(
                        result.get("metriques_toves"), ensure_ascii=False
                    ),
                    json.dumps(
                        result.get("validacio_funcional"), ensure_ascii=False
                    ),
                    json.dumps(
                        result["candidats_multillavor"], ensure_ascii=False
                    ),
                ),
            ).lastrowid
        )
        for item_type, items in (
            ("assignacio", result["assignacions"]),
            ("descoberta", result["descobertes"]),
        ):
            connection.executemany(
                """
                INSERT INTO proposta_inicial_cp_sat_elements
                (proposta_id, tipus, necessitat_id, data, servei,
                 treballador_id, treballador, hora_inici, hora_fi,
                 durada_hores, zona, torn_requerit, habilitacions,
                 motiu_descobert, diagnostic_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        draft_id,
                        item_type,
                        item["necessitat_id"],
                        item["data"],
                        item["servei"],
                        item.get("treballador_id"),
                        item.get("treballador"),
                        item["hora_inici"],
                        item["hora_fi"],
                        item["durada_hores"],
                        item.get("zona"),
                        item.get("torn_requerit"),
                        item.get("habilitacions"),
                        item.get("motiu_descobert"),
                        json.dumps(
                            item.get("diagnostic_descobert"),
                            ensure_ascii=False,
                        ),
                    )
                    for item in items
                ],
            )
    return draft_id


def list_initial_coverage_drafts(
    database_path: str | Path,
    *,
    include_discarded: bool = False,
) -> list[dict[str, Any]]:
    initialize_cp_sat_drafts(database_path)
    where = "" if include_discarded else "WHERE estat <> 'descartada'"
    with closing(_readonly_connection(database_path)) as connection:
        rows = connection.execute(
            f"""
            SELECT id, estat, data_inici, data_fi, created_at, updated_at,
                   solver_status, necessitats_cobertes, necessitats_totals,
                   temps_total_segons, llavor_seleccionada, snapshot_hash
            FROM propostes_inicials_cp_sat
            {where}
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def load_initial_coverage_draft(
    database_path: str | Path,
    draft_id: int,
) -> dict[str, Any]:
    initialize_cp_sat_drafts(database_path)
    with closing(_readonly_connection(database_path)) as connection:
        header = connection.execute(
            "SELECT * FROM propostes_inicials_cp_sat WHERE id = ?",
            (draft_id,),
        ).fetchone()
        if header is None:
            raise ValueError("No s'ha trobat l'esborrany CP-SAT")
        elements = connection.execute(
            """
            SELECT * FROM proposta_inicial_cp_sat_elements
            WHERE proposta_id = ?
            ORDER BY data, servei, id
            """,
            (draft_id,),
        ).fetchall()
        publication = connection.execute(
            """
            SELECT id, estat, backup_path, assignacions_publicades,
                   assignacions_previes_anullades, created_at, rolled_back_at
            FROM publicacions_inicials_cp_sat
            WHERE proposta_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (draft_id,),
        ).fetchone()

    assignments: list[dict[str, Any]] = []
    uncovered: list[dict[str, Any]] = []
    for row in elements:
        item = {
            "data": row["data"],
            "servei": row["servei"],
            "necessitat_id": row["necessitat_id"],
            "hora_inici": row["hora_inici"],
            "hora_fi": row["hora_fi"],
            "durada_hores": row["durada_hores"],
            "zona": row["zona"] or "",
            "torn_requerit": row["torn_requerit"] or "",
            "habilitacions": row["habilitacions"] or "",
        }
        if row["tipus"] == "assignacio":
            item["treballador_id"] = row["treballador_id"]
            item["treballador"] = row["treballador"]
            assignments.append(item)
        else:
            item["motiu_descobert"] = row["motiu_descobert"] or ""
            diagnostic = (
                json.loads(row["diagnostic_json"])
                if row["diagnostic_json"]
                else None
            )
            item["candidats_estatics"] = (
                diagnostic.get("candidats_estatics") if diagnostic else None
            )
            item["candidats_lliures"] = (
                diagnostic.get("compatibles_amb_proposta")
                if diagnostic
                else None
            )
            item["diagnostic_descobert"] = diagnostic
            uncovered.append(item)

    return {
        "esborrany_id": int(header["id"]),
        "estat_esborrany": header["estat"],
        "created_at": header["created_at"],
        "updated_at": header["updated_at"],
        "data_inici": header["data_inici"],
        "data_fi": header["data_fi"],
        "estat": header["solver_status"],
        "factible": header["solver_status"] in {"FEASIBLE", "OPTIMAL"},
        "necessitats_cobertes": header["necessitats_cobertes"],
        "necessitats_totals": header["necessitats_totals"],
        "temps_total_segons": header["temps_total_segons"],
        "llavor_seleccionada": header["llavor_seleccionada"],
        "aturada_primera_llavor": bool(
            header["aturada_primera_llavor"]
        ),
        "configuracio": json.loads(header["configuracio_json"]),
        "llavors_demanades": json.loads(header["llavors_json"]),
        "snapshot_hash": header["snapshot_hash"],
        "operational_snapshot_hash": header["operational_snapshot_hash"],
        "assignacions": assignments,
        "descobertes": uncovered,
        "fases": json.loads(header["fases_json"]),
        "metriques_toves": (
            json.loads(header["metriques_json"])
            if header["metriques_json"]
            else None
        ),
        "validacio_funcional": (
            json.loads(header["validacio_funcional_json"])
            if header["validacio_funcional_json"]
            else None
        ),
        "candidats_multillavor": json.loads(header["candidats_json"]),
        "publicada": header["estat"] == "publicada",
        "publicacio": dict(publication) if publication else None,
    }


def _revalidate_draft_data(
    database_path: str | Path,
    draft: dict[str, Any],
) -> tuple[PlanningProblem, list[Any]]:
    """Reconstrueix i valida una proposta contra les dades vigents."""
    from cp_sat_pilot import Assignment, CpSatPlanner
    from cp_sat_pilot.sqlite_adapter import (
        SqliteInputError,
        load_problem_from_sqlite,
    )

    start_date = date.fromisoformat(draft["data_inici"])
    end_date = date.fromisoformat(draft["data_fi"])
    try:
        with redirect_stdout(StringIO()):
            problem = load_problem_from_sqlite(
                database_path,
                start_date=start_date,
                end_date=end_date,
                duplicate_policy="replace_all",
            )
    except SqliteInputError as exc:
        raise ValueError(str(exc)) from exc

    if _problem_snapshot_hash(problem) != draft["snapshot_hash"]:
        raise ValueError(
            "Les necessitats, descansos, habilitacions o l'històric han "
            "canviat des de la generació. Cal crear un esborrany nou."
        )

    needs = {need.id: need for need in problem.needs}
    problem_need_ids = set(needs)
    draft_need_ids = {
        item["necessitat_id"]
        for item in (*draft["assignacions"], *draft["descobertes"])
    }
    if draft_need_ids != problem_need_ids:
        raise ValueError(
            "L'esborrany no conté exactament les necessitats del període"
        )

    assignments = []
    for item in draft["assignacions"]:
        need = needs[item["necessitat_id"]]
        assignments.append(
            Assignment(
                worker_id=str(item["treballador_id"]),
                need_id=need.id,
                service_id=need.service_id,
                date=need.date,
                start=need.start,
                end=need.end,
                duration_minutes=need.duration_minutes,
            )
        )
    errors = CpSatPlanner(problem).validate(assignments)
    if errors:
        preview = "; ".join(errors[:5])
        suffix = f"; i {len(errors) - 5} més" if len(errors) > 5 else ""
        raise ValueError(
            f"La proposta incompleix restriccions rígides: {preview}{suffix}"
        )

    coverage_phase = next(
        (phase for phase in draft["fases"] if phase["name"] == "cobertura"),
        None,
    )
    if (
        coverage_phase is None
        or coverage_phase["status"] != "OPTIMAL"
        or round(coverage_phase.get("objective_value") or 0)
        != len(assignments)
    ):
        raise ValueError(
            "No consta una prova òptima de la cobertura màxima; "
            "cal recalcular la proposta."
        )
    return problem, assignments


def validate_initial_coverage_draft(
    database_path: str | Path,
    draft_id: int,
) -> dict[str, Any]:
    """Revalida un esborrany immutable i el marca com a validat."""
    draft = load_initial_coverage_draft(database_path, draft_id)
    if draft["estat_esborrany"] != "esborrany":
        raise ValueError("Només es pot validar una proposta en esborrany")

    problem, assignments = _revalidate_draft_data(database_path, draft)
    start_date = date.fromisoformat(draft["data_inici"])
    end_date = date.fromisoformat(draft["data_fi"])
    operational_hash = _operational_snapshot_hash(
        database_path, start_date, end_date
    )

    with closing(_write_connection(database_path)) as connection, connection:
        cursor = connection.execute(
            """
            UPDATE propostes_inicials_cp_sat
            SET estat = 'validada', operational_snapshot_hash = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND estat = 'esborrany' AND snapshot_hash = ?
            """,
            (operational_hash, draft_id, draft["snapshot_hash"]),
        )
        if cursor.rowcount != 1:
            raise ValueError(
                "L'esborrany ha canviat durant la validació; torna'l a obrir"
            )
    return {
        "esborrany_id": draft_id,
        "estat": "validada",
        "necessitats_cobertes": len(assignments),
        "necessitats_totals": len(problem.needs),
        "errors_validacio": 0,
        "operational_snapshot_hash": operational_hash,
    }


def _separated_values(value: object) -> set[str]:
    if not value:
        return set()
    return {
        part.strip().lower().replace("í", "i")
        for part in str(value).replace("+", ",").replace(";", ",").split(",")
        if part.strip()
    }


def _turn_change(
    worker_rotation: object,
    required_rotation: object,
    required_turn: object,
) -> int:
    required = _separated_values(required_rotation or required_turn)
    worker_turns = _separated_values(worker_rotation)
    return int(
        bool(required and worker_turns and required.isdisjoint(worker_turns))
    )


def publish_initial_coverage_draft(
    database_path: str | Path,
    draft_id: int,
    *,
    backup_directory: str | Path | None = None,
) -> dict[str, Any]:
    """Publica tota una proposta validada en una transacció reversible."""
    initialize_cp_sat_drafts(database_path)
    draft = load_initial_coverage_draft(database_path, draft_id)
    if draft["estat_esborrany"] != "validada":
        raise ValueError("Només es pot publicar una proposta validada")

    start_date = date.fromisoformat(draft["data_inici"])
    end_date = date.fromisoformat(draft["data_fi"])
    backup_path: Path | None = None
    connection = _write_connection(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        header = connection.execute(
            """
            SELECT estat, snapshot_hash, operational_snapshot_hash
            FROM propostes_inicials_cp_sat
            WHERE id = ?
            """,
            (draft_id,),
        ).fetchone()
        if header is None or header["estat"] != "validada":
            raise ValueError(
                "La proposta ja no està disponible per publicar"
            )
        if header["snapshot_hash"] != draft["snapshot_hash"]:
            raise ValueError(
                "La proposta ha canviat des que es va obrir; torna-la a obrir"
            )

        problem, assignments = _revalidate_draft_data(database_path, draft)
        operational_before = _operational_snapshot_hash_connection(
            connection, start_date, end_date
        )
        validated_operational_hash = header["operational_snapshot_hash"]
        if (
            validated_operational_hash
            and operational_before != validated_operational_hash
        ):
            raise ValueError(
                "El pla operatiu ha canviat des de la validació. "
                "Cal generar i validar una proposta nova."
            )

        need_ids = {need.id for need in problem.needs}
        active_rows = connection.execute(
            """
            SELECT * FROM assig_grup_T
            WHERE data BETWEEN ? AND ?
              AND estat_planificacio IN ('publicada', 'bloquejada')
            ORDER BY id
            """,
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
        unknown_active = [
            row
            for row in active_rows
            if f"{row['data']}::{row['torn']}" not in need_ids
        ]
        if unknown_active:
            raise ValueError(
                "El període conté assignacions actives sense una necessitat "
                "equivalent. No es poden substituir automàticament."
            )
        if any(row["estat_planificacio"] == "bloquejada" for row in active_rows):
            raise ValueError(
                "El període conté assignacions bloquejades. Cal desbloquejar-les "
                "o generar una proposta que les conservi."
            )
        active_need_ids = [f"{row['data']}::{row['torn']}" for row in active_rows]
        if len(active_need_ids) != len(set(active_need_ids)):
            raise ValueError(
                "El pla operatiu conté més d'una cobertura activa per necessitat"
            )

        backup_path = _create_sqlite_backup(
            database_path, draft_id, backup_directory
        )

        previous_assignments = [
            {
                "id": int(row["id"]),
                "estat_planificacio": row["estat_planificacio"],
            }
            for row in active_rows
        ]
        if active_rows:
            connection.executemany(
                """
                UPDATE assig_grup_T
                SET estat_planificacio = 'anul_lada'
                WHERE id = ? AND estat_planificacio = ?
                """,
                [
                    (row["id"], row["estat_planificacio"])
                    for row in active_rows
                ],
            )

        deleted_history: list[dict[str, Any]] = []
        for need in problem.needs:
            rows = connection.execute(
                """
                SELECT rowid AS _rowid, * FROM historic_assignacions
                WHERE data = ? AND torn_id = ?
                ORDER BY rowid
                """,
                (need.date.isoformat(), need.service_id),
            ).fetchall()
            deleted_history.extend(_rows_as_dicts(rows))
            connection.execute(
                """
                DELETE FROM historic_assignacions
                WHERE data = ? AND torn_id = ?
                """,
                (need.date.isoformat(), need.service_id),
            )

        assignment_by_need = {
            assignment.need_id: assignment for assignment in assignments
        }
        need_by_id = {need.id: need for need in problem.needs}
        day_names = ("Dl", "Dt", "Dc", "Dj", "Dv", "Ds", "Dg")
        inserted_assignment_ids: list[int] = []
        inserted_history_rowids: list[int] = []
        for need_id in sorted(assignment_by_need):
            assignment = assignment_by_need[need_id]
            need = need_by_id[need_id]
            worker = connection.execute(
                """
                SELECT id, treballador, plaza, rotacio, zona, grup
                FROM treballadors
                WHERE CAST(id AS TEXT) = CAST(? AS TEXT)
                """,
                (assignment.worker_id,),
            ).fetchone()
            if worker is None or worker["grup"] != "T":
                raise ValueError(
                    f"Treballador proposat invàlid: {assignment.worker_id}"
                )
            coverage = connection.execute(
                """
                SELECT linia, zona, formacio, rotacio, torn
                FROM cobertura
                WHERE data = ? AND servei = ?
                LIMIT 1
                """,
                (need.date.isoformat(), need.service_id),
            ).fetchone()
            if coverage is None:
                raise ValueError(
                    f"La necessitat {need_id} ja no existeix a cobertura"
                )
            zone = coverage["zona"] or need.zone
            zone_change = int(
                bool(worker["zona"] and zone and worker["zona"] != zone)
            )
            turn_change = _turn_change(
                worker["rotacio"], coverage["rotacio"], coverage["torn"]
            )
            historic_hours = float(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(durada_hores), 0)
                    FROM historic_assignacions
                    WHERE CAST(treballador_id AS TEXT) = CAST(? AS TEXT)
                    """,
                    (worker["id"],),
                ).fetchone()[0]
                or 0
            )
            duration_hours = assignment.duration_minutes / 60
            assignment_id = int(
                connection.execute(
                    """
                    INSERT INTO assig_grup_T
                    (data, dia_setmana, torn, treballador_id,
                     treballador_nom, treballador_plaza, treballador_grup,
                     hora_inici, hora_fi, durada_hores, linia, zona,
                     formacio, es_canvi_zona, es_canvi_torn,
                     hores_totals_any, estat_planificacio)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            'publicada')
                    """,
                    (
                        need.date.isoformat(),
                        day_names[need.date.weekday()],
                        need.service_id,
                        str(worker["id"]),
                        worker["treballador"],
                        worker["plaza"],
                        worker["grup"],
                        assignment.start.strftime("%H:%M"),
                        assignment.end.strftime("%H:%M"),
                        duration_hours,
                        coverage["linia"] or "",
                        zone,
                        coverage["formacio"] or "",
                        zone_change,
                        turn_change,
                        historic_hours + duration_hours,
                    ),
                ).lastrowid
            )
            history_rowid = int(
                connection.execute(
                    """
                    INSERT INTO historic_assignacions
                    (treballador_id, torn_id, data, hora_inici, hora_fi,
                     durada_hores, es_canvi_zona, es_canvi_torn, data_apunt)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        str(worker["id"]),
                        need.service_id,
                        need.date.isoformat(),
                        assignment.start.strftime("%H:%M"),
                        assignment.end.strftime("%H:%M"),
                        duration_hours,
                        zone_change,
                        turn_change,
                    ),
                ).lastrowid
            )
            inserted_assignment_ids.append(assignment_id)
            inserted_history_rowids.append(history_rowid)

        rollback_data = {
            "previous_assignments": previous_assignments,
            "inserted_assignment_ids": inserted_assignment_ids,
            "deleted_history": deleted_history,
            "inserted_history_rowids": inserted_history_rowids,
        }
        operational_after = _operational_snapshot_hash_connection(
            connection, start_date, end_date
        )
        publication_id = int(
            connection.execute(
                """
                INSERT INTO publicacions_inicials_cp_sat
                (proposta_id, snapshot_hash, operational_snapshot_before,
                 operational_snapshot_after, backup_path,
                 assignacions_publicades, assignacions_previes_anullades,
                 rollback_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    draft["snapshot_hash"],
                    operational_before,
                    operational_after,
                    str(backup_path),
                    len(inserted_assignment_ids),
                    len(previous_assignments),
                    json.dumps(rollback_data, ensure_ascii=False),
                ),
            ).lastrowid
        )
        cursor = connection.execute(
            """
            UPDATE propostes_inicials_cp_sat
            SET estat = 'publicada', updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND estat = 'validada'
            """,
            (draft_id,),
        )
        if cursor.rowcount != 1:
            raise ValueError(
                "La proposta ha canviat durant la publicació"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return {
        "publicacio_id": publication_id,
        "esborrany_id": draft_id,
        "estat": "publicada",
        "assignacions_publicades": len(inserted_assignment_ids),
        "assignacions_previes_anullades": len(previous_assignments),
        "backup_path": str(backup_path),
    }


def rollback_initial_coverage_publication(
    database_path: str | Path,
    draft_id: int,
) -> dict[str, Any]:
    """Reverteix només les files de l'última publicació, atòmicament."""
    initialize_cp_sat_drafts(database_path)
    connection = _write_connection(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        draft = connection.execute(
            """
            SELECT estat, data_inici, data_fi
            FROM propostes_inicials_cp_sat
            WHERE id = ?
            """,
            (draft_id,),
        ).fetchone()
        publication = connection.execute(
            """
            SELECT * FROM publicacions_inicials_cp_sat
            WHERE proposta_id = ? AND estat = 'publicada'
            ORDER BY id DESC LIMIT 1
            """,
            (draft_id,),
        ).fetchone()
        if (
            draft is None
            or draft["estat"] != "publicada"
            or publication is None
        ):
            raise ValueError("La proposta no té cap publicació per revertir")

        start_date = date.fromisoformat(draft["data_inici"])
        end_date = date.fromisoformat(draft["data_fi"])
        current_hash = _operational_snapshot_hash_connection(
            connection, start_date, end_date
        )
        if current_hash != publication["operational_snapshot_after"]:
            raise ValueError(
                "El pla ha canviat després de la publicació. El rollback "
                "automàtic s'ha bloquejat per no perdre canvis posteriors."
            )

        rollback_data = json.loads(publication["rollback_json"])
        connection.executemany(
            "DELETE FROM historic_assignacions WHERE rowid = ?",
            [
                (rowid,)
                for rowid in rollback_data["inserted_history_rowids"]
            ],
        )
        connection.executemany(
            "DELETE FROM assig_grup_T WHERE id = ?",
            [
                (assignment_id,)
                for assignment_id in rollback_data["inserted_assignment_ids"]
            ],
        )
        history_columns = (
            "treballador_id", "torn_id", "data", "hora_inici", "hora_fi",
            "durada_hores", "es_canvi_zona", "es_canvi_torn", "data_apunt",
        )
        for row in rollback_data["deleted_history"]:
            connection.execute(
                """
                INSERT INTO historic_assignacions
                (rowid, treballador_id, torn_id, data, hora_inici, hora_fi,
                 durada_hores, es_canvi_zona, es_canvi_torn, data_apunt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (row["_rowid"], *(row[column] for column in history_columns)),
            )
        connection.executemany(
            """
            UPDATE assig_grup_T
            SET estat_planificacio = ?
            WHERE id = ? AND estat_planificacio = 'anul_lada'
            """,
            [
                (row["estat_planificacio"], row["id"])
                for row in rollback_data["previous_assignments"]
            ],
        )
        restored_hash = _operational_snapshot_hash_connection(
            connection, start_date, end_date
        )
        if restored_hash != publication["operational_snapshot_before"]:
            raise ValueError(
                "El rollback no ha reconstruït exactament el pla anterior"
            )
        connection.execute(
            """
            UPDATE publicacions_inicials_cp_sat
            SET estat = 'revertida', rolled_back_at = CURRENT_TIMESTAMP
            WHERE id = ? AND estat = 'publicada'
            """,
            (publication["id"],),
        )
        connection.execute(
            """
            UPDATE propostes_inicials_cp_sat
            SET estat = 'validada', operational_snapshot_hash = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND estat = 'publicada'
            """,
            (restored_hash, draft_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return {
        "publicacio_id": int(publication["id"]),
        "esborrany_id": draft_id,
        "estat": "validada",
        "assignacions_retirades": len(
            rollback_data["inserted_assignment_ids"]
        ),
        "assignacions_previes_restaurades": len(
            rollback_data["previous_assignments"]
        ),
        "backup_path": publication["backup_path"],
    }


def discard_initial_coverage_draft(
    database_path: str | Path,
    draft_id: int,
) -> None:
    initialize_cp_sat_drafts(database_path)
    with closing(_write_connection(database_path)) as connection, connection:
        cursor = connection.execute(
            """
            UPDATE propostes_inicials_cp_sat
            SET estat = 'descartada', updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND estat IN ('esborrany', 'validada')
            """,
            (draft_id,),
        )
        if cursor.rowcount != 1:
            raise ValueError("La proposta no està disponible per descartar")
