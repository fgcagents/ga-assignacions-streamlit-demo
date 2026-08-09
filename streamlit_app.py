"""Aplicació Streamlit unificada de planificació i operativa CP-SAT."""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
CP_SAT_SOURCE_DIR = BASE_DIR / "cp_sat_pilot" / "src"
if CP_SAT_SOURCE_DIR.is_dir():
    source_path = str(CP_SAT_SOURCE_DIR)
    if source_path not in sys.path:
        sys.path.insert(0, source_path)
    loaded_cp_sat = sys.modules.get("cp_sat_pilot")
    if (
        loaded_cp_sat is not None
        and getattr(loaded_cp_sat, "__file__", None) is None
    ):
        del sys.modules["cp_sat_pilot"]

import streamlit as st

from planificador_cp_sat.ui.descansos import (
    reinicia_formularis_descansos,
)
from planificador_cp_sat.services.esquema_planificacio import (
    PlanningSchemaMigrationError,
    migrate_planning_schema,
)

DEMO_DATABASE_PATH = BASE_DIR / "data" / "treballadors_demo.db"


def _session_database_path() -> Path:
    """Aïlla els canvis de cada sessió sobre una còpia de la base demo."""
    configured_path = os.environ.get("PLANIFICADOR_DATABASE_PATH")
    if configured_path:
        return Path(configured_path)
    if not DEMO_DATABASE_PATH.exists():
        return DEMO_DATABASE_PATH

    session_path = st.session_state.get("deploy_database_path")
    if session_path:
        return Path(session_path)

    session_directory = Path(tempfile.mkdtemp(prefix="cp_sat_demo_"))
    session_path = session_directory / "treballadors_demo.db"
    shutil.copy2(DEMO_DATABASE_PATH, session_path)
    st.session_state["deploy_database_path"] = str(session_path)
    return session_path


DATABASE_PATH = _session_database_path()

st.set_page_config(
    page_title="Planificador de cobertures",
    page_icon=":material/calendar_month:",
    layout="wide",
)


@st.cache_resource
def _prepare_schema(database_path: str) -> None:
    """Prepara una vegada les taules compartides per totes les pàgines."""
    migrate_planning_schema(database_path)


with st.sidebar:
    st.subheader("Dades de treball")
    if DATABASE_PATH.exists():
        st.badge(
            "Base de dades disponible",
            icon=":material/check_circle:",
            color="green",
        )
        st.caption(DATABASE_PATH.name)
        if not os.environ.get("PLANIFICADOR_DATABASE_PATH"):
            st.caption("Còpia temporal independent per a aquesta sessió.")
    else:
        st.error(
            f"No s'ha trobat {DATABASE_PATH}",
            icon=":material/error:",
        )
    if st.button(
        "Actualitzar dades",
        icon=":material/refresh:",
        width="stretch",
        help="Neteja la memòria temporal i torna a llegir SQLite.",
    ):
        reinicia_formularis_descansos()
        st.cache_data.clear()
        st.rerun()
    st.caption(
        "Generar o validar una proposta no modifica el pla publicat."
    )

if not DATABASE_PATH.exists():
    st.error(
        "No es pot iniciar el planificador sense la base de dades.",
        icon=":material/database_off:",
    )
    st.stop()

try:
    _prepare_schema(str(DATABASE_PATH))
except (
    OSError,
    sqlite3.Error,
    ValueError,
    PlanningSchemaMigrationError,
) as error:
    st.error(
        f"No s'ha pogut preparar la base de dades: {error}",
        icon=":material/database_off:",
    )
    st.stop()

st.session_state["database_path"] = str(DATABASE_PATH)

page = st.navigation(
    [
        st.Page(
            "app_pages/resum.py",
            title="Resum",
            icon=":material/dashboard:",
            default=True,
        ),
        st.Page(
            "app_pages/planificacio.py",
            title="Planificació",
            icon=":material/calendar_month:",
        ),
        st.Page(
            "app_pages/pla_publicat.py",
            title="Pla publicat",
            icon=":material/fact_check:",
        ),
        st.Page(
            "app_pages/personal.py",
            title="Personal",
            icon=":material/groups:",
        ),
        st.Page(
            "app_pages/incidencies.py",
            title="Incidències",
            icon=":material/report:",
        ),
    ],
    position="top",
)
page.run()
