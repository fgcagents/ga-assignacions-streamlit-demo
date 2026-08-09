"""Comprova que la còpia desplegable és completa i no inclou dades indegudes."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
DEMO_DATABASE = ROOT / "data" / "treballadors_demo.db"
REQUIRED_PATHS = (
    ROOT / "streamlit_app.py",
    ROOT / "requirements.txt",
    ROOT / "app_pages" / "resum.py",
    ROOT / "app_pages" / "planificacio.py",
    ROOT / "app_pages" / "pla_publicat.py",
    ROOT / "app_pages" / "personal.py",
    ROOT / "app_pages" / "incidencies.py",
    ROOT / "planificador_cp_sat" / "ui" / "dashboard.py",
    ROOT / "planificador_cp_sat" / "ui" / "planificacio.py",
    ROOT / "planificador_cp_sat" / "services" / "esquema_planificacio.py",
    ROOT / "planificador_cp_sat" / "services" / "replanificacio.py",
    ROOT / "scripts" / "create_demo_database.py",
    ROOT / "cp_sat_pilot" / "src" / "cp_sat_pilot" / "model.py",
    ROOT / "cp_sat_pilot" / "src" / "cp_sat_pilot" / "sqlite_adapter.py",
)
FORBIDDEN_DIRECTORIES = {"backups", "copies", ".venv", "__pycache__"}


def _verify_demo_database() -> list[str]:
    errors: list[str] = []
    encoded = quote(DEMO_DATABASE.resolve().as_posix(), safe="/:")
    try:
        connection = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            errors.append(f"La base demo no és íntegra: {integrity}")

        total, synthetic_ids, synthetic_names, operational_places = (
            connection.execute(
                """
                SELECT COUNT(*),
                       SUM(CASE WHEN id >= 10001 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN treballador GLOB 'Persona D[0-9][0-9][0-9]' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN plaza IS NOT NULL AND TRIM(plaza) <> '' THEN 1 ELSE 0 END)
                FROM treballadors
                """
            ).fetchone()
        )
        if not total or (synthetic_ids, synthetic_names, operational_places) != (
            total,
            total,
            total,
        ):
            errors.append(
                "La base demo conté identitats que no segueixen el patró sintètic"
            )

        for table in (
            "auditoria_planificacio",
            "incidencies_personal",
            "propostes_inicials_cp_sat",
            "propostes_replanificacio",
        ):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if exists and connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]:
                errors.append(f"La base demo conserva estat operatiu a {table}")
        connection.close()
    except sqlite3.Error as error:
        errors.append(f"No es pot validar la base demo: {error}")
    return errors


def verify(require_demo_data: bool = False) -> list[str]:
    errors: list[str] = []
    for path in REQUIRED_PATHS:
        if not path.exists():
            errors.append(f"Falta el fitxer requerit: {path.relative_to(ROOT)}")

    for directory in ROOT.rglob("*"):
        if directory.is_dir() and directory.name in FORBIDDEN_DIRECTORIES:
            errors.append(
                f"Directori no publicable: {directory.relative_to(ROOT)}"
            )

    sqlite_files = {
        path.resolve()
        for pattern in ("*.db", "*.sqlite", "*.sqlite3")
        for path in ROOT.rglob(pattern)
    }
    unexpected_databases = sqlite_files - {DEMO_DATABASE.resolve()}
    for path in sorted(unexpected_databases):
        errors.append(f"Base no autoritzada: {path.relative_to(ROOT)}")

    if require_demo_data and not DEMO_DATABASE.exists():
        errors.append("Falta data/treballadors_demo.db")
    if DEMO_DATABASE.exists():
        errors.extend(_verify_demo_database())

    for path in ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except (OSError, SyntaxError, UnicodeError) as error:
            errors.append(f"No es pot compilar {path.relative_to(ROOT)}: {error}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--require-demo-data",
        action="store_true",
        help="Exigeix la base pseudonimitzada abans de publicar.",
    )
    arguments = parser.parse_args()
    errors = verify(arguments.require_demo_data)
    if errors:
        print("Còpia no preparada:")
        for error in errors:
            print(f"- {error}")
        return 1
    if DEMO_DATABASE.exists():
        print("Còpia preparada amb base de demostració.")
    else:
        print("Codi preparat. Falta afegir i revisar la base de demostració.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
