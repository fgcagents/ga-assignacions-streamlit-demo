"""Reinicia l'estat operatiu d'una còpia anual sense tocar les entrades.

L'eina exigeix una base d'escenari diferent de ``treballadors.db``, crea un
backup SQLite abans de modificar-la i comprova que cobertura, assignacions A,
descansos i dades mestres siguin lògicament idèntics abans i després.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path


PRESERVED_TABLES = (
    "treballadors",
    "serveis",
    "serveis_calendari",
    "serveis_horaris",
    "cobertura",
    "assig_grup_A",
    "descansos_dies",
    "migracions_planificacio_cp_sat",
)

# Ordre de fills a pares per respectar les claus foranes.
RESET_TABLES = (
    "proposta_canvis",
    "propostes_replanificacio",
    "incidencies_personal",
    "proposta_inicial_cp_sat_elements",
    "publicacions_inicials_cp_sat",
    "propostes_inicials_cp_sat",
    "versions_pla_publicat",
    "publicacions_planificacio_cp_sat",
    "canvis_planificacio_cp_sat",
    "execucions_planificacio_cp_sat",
    "bloquejos_planificacio",
    "preassignacions_planificacio",
    "auditoria_planificacio",
    "ajustos_descans_substitucio",
    "hores_realitzades",
    "tancaments_hores_realitzades",
    "assig_grup_T",
    "historic_assignacions",
)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _table_digest(connection: sqlite3.Connection, table: str) -> str:
    digest = hashlib.sha256()
    quoted = _quote_identifier(table)
    columns = [
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({quoted})")
    ]
    query = f"SELECT * FROM {quoted} ORDER BY rowid"
    for row in connection.execute(query):
        payload = [row[column] for column in columns]
        digest.update(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _backup_database(database: Path, backup_directory: Path) -> Path:
    backup_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = backup_directory / f"{database.stem}_abans_reinici_{timestamp}.db"
    if backup.exists():
        raise FileExistsError(f"El backup ja existeix: {backup}")
    with closing(
        sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    ) as source:
        with closing(sqlite3.connect(backup)) as destination:
            source.backup(destination)
    return backup


def reset_state(
    database: Path,
    backup_directory: Path,
    report_path: Path,
) -> dict[str, object]:
    database = database.resolve()
    if not database.is_file():
        raise FileNotFoundError(f"No existeix la base: {database}")
    if database.name.casefold() == "treballadors.db":
        raise ValueError("Es rebutja reiniciar la base operativa treballadors.db")
    if "absentisme" not in database.stem.casefold():
        raise ValueError(
            "La base de proves ha d'incloure 'absentisme' al nom per seguretat"
        )

    with closing(
        sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    ) as readonly:
        readonly.row_factory = sqlite3.Row
        missing = [
            table for table in PRESERVED_TABLES if not _table_exists(readonly, table)
        ]
        if missing:
            raise ValueError("Falten taules preservades: " + ", ".join(missing))
        synthetic_absences = int(
            readonly.execute(
                """
                SELECT COUNT(*) FROM descansos_dies
                WHERE origen = 'baixa' AND motiu LIKE 'SIM2025 | %'
                """
            ).fetchone()[0]
        )
        coverage_count = int(readonly.execute("SELECT COUNT(*) FROM cobertura").fetchone()[0])
        if synthetic_absences != 1496:
            raise ValueError(
                "La base no conté els 1.496 dies de baixa sintètics esperats"
            )
        if coverage_count <= 0:
            raise ValueError("La còpia no conté cobertura generada")
        before_digests = {
            table: _table_digest(readonly, table) for table in PRESERVED_TABLES
        }
        before_counts = {
            table: int(
                readonly.execute(
                    f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                ).fetchone()[0]
            )
            for table in PRESERVED_TABLES
        }
        reset_before = {
            table: int(
                readonly.execute(
                    f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                ).fetchone()[0]
            )
            for table in RESET_TABLES
            if _table_exists(readonly, table)
        }

    backup = _backup_database(database, backup_directory.resolve())

    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            reset_counts: dict[str, int] = {}
            for table in RESET_TABLES:
                if not _table_exists(connection, table):
                    continue
                cursor = connection.execute(
                    f"DELETE FROM {_quote_identifier(table)}"
                )
                reset_counts[table] = int(cursor.rowcount)

            after_digests = {
                table: _table_digest(connection, table)
                for table in PRESERVED_TABLES
            }
            changed_preserved = [
                table
                for table in PRESERVED_TABLES
                if after_digests[table] != before_digests[table]
            ]
            remaining = {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                    ).fetchone()[0]
                )
                for table in reset_counts
            }
            duplicates = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT treballador_id, data
                        FROM descansos_dies
                        GROUP BY treballador_id, data
                        HAVING COUNT(*) > 1
                    )
                    """
                ).fetchone()[0]
            )
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_key_errors = [
                tuple(row) for row in connection.execute("PRAGMA foreign_key_check")
            ]
            if changed_preserved:
                raise RuntimeError(
                    "S'han modificat taules preservades: "
                    + ", ".join(changed_preserved)
                )
            if any(remaining.values()):
                raise RuntimeError(f"Han quedat files operatives: {remaining}")
            if duplicates:
                raise RuntimeError("Han reaparegut descansos persona-dia duplicats")
            if integrity != "ok" or foreign_key_errors:
                raise RuntimeError(
                    f"Validació SQLite incorrecta: {integrity}; "
                    f"FK={foreign_key_errors[:5]}"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    report: dict[str, object] = {
        "database": str(database),
        "backup": str(backup),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "preserved_counts": before_counts,
        "preserved_digests": before_digests,
        "reset_before": reset_before,
        "deleted_rows": reset_counts,
        "reset_after": remaining,
        "synthetic_absence_days": synthetic_absences,
        "coverage_rows": coverage_count,
        "rest_person_day_duplicates": 0,
        "sqlite_integrity_check": "ok",
        "foreign_key_errors": 0,
    }
    report_path = report_path.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--backup-directory", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = reset_state(
        args.database,
        args.backup_directory,
        args.report,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
