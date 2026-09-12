"""Comprova que la còpia desplegable és completa i no inclou dades indegudes."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
DEMO_DATABASE = ROOT / "data" / "treballadors_demo.db"
REQUIRED_PATHS = (
    ROOT / "planificador_cp_sat" / "ui" / "ajuda.py",
    ROOT / "docs" / "GUIA_USUARI_CP_SAT.html",
    ROOT / "streamlit_app.py",
    ROOT / "requirements.txt",
    ROOT / "app_pages" / "resum.py",
    ROOT / "app_pages" / "planificacio.py",
    ROOT / "app_pages" / "pla_publicat.py",
    ROOT / "app_pages" / "cronograma.py",
    ROOT / "app_pages" / "hores_realitzades.py",
    ROOT / "app_pages" / "personal.py",
    ROOT / "app_pages" / "incidencies.py",
    ROOT / "planificador_cp_sat" / "ui" / "dashboard.py",
    ROOT / "planificador_cp_sat" / "ui" / "cronograma.py",
    ROOT / "planificador_cp_sat" / "ui" / "planificacio.py",
    ROOT / "planificador_cp_sat" / "services" / "esquema_planificacio.py",
    ROOT / "planificador_cp_sat" / "services" / "cronograma.py",
    ROOT / "planificador_cp_sat" / "services" / "replanificacio.py",
    ROOT / "planificador_cp_sat" / "services" / "hores_realitzades.py",
    ROOT / "planificador_cp_sat" / "solver_engines" / "__init__.py",
    ROOT / "scripts" / "create_demo_database.py",
    ROOT / "scripts" / "reset_annual_test_state.py",
    ROOT / "cp_sat_pilot" / "src" / "cp_sat_pilot" / "model.py",
    ROOT / "cp_sat_pilot" / "src" / "cp_sat_pilot" / "domain.py",
    ROOT / "cp_sat_pilot" / "src" / "cp_sat_pilot" / "quality.py",
    ROOT / "cp_sat_pilot" / "src" / "cp_sat_pilot" / "priority_planner.py",
    ROOT / "cp_sat_pilot" / "src" / "cp_sat_pilot" / "sqlite_adapter.py",
    ROOT
    / "cp_sat_pilot"
    / "src"
    / "cp_sat_pilot"
    / "constraints"
    / "soft"
    / "equity.py",
    ROOT / "planificador_cp_sat" / "services" / "persistencia_planificacio.py",
    ROOT / "planificador_cp_sat" / "services" / "publicacio_planificacio.py",
)
FORBIDDEN_DIRECTORIES = {"backups", "copies", ".venv", "__pycache__"}
REQUIRED_APP_DEFAULTS = (
    'os.environ.setdefault("PLANIFICACIO_INCREMENTAL_MODE", "active")',
    '"PLANIFICACIO_INCREMENTAL_PUBLICATION_ENABLED",',
    '"true",',
)
RESET_STATE_TABLES = (
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

        for table in RESET_STATE_TABLES:
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

    app_source = (ROOT / "streamlit_app.py").read_text(encoding="utf-8")
    for expected in REQUIRED_APP_DEFAULTS:
        if expected not in app_source:
            errors.append(
                "Falta la configuració activa de desplegament a streamlit_app.py: "
                f"{expected}"
            )

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
