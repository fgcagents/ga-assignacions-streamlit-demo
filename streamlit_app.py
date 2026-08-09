"""Aplicació Streamlit unificada de planificació i operativa CP-SAT."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import streamlit as st

from ui_descansos import (
    reinicia_formularis_descansos,
    render_pestanya_descansos,
)
from ui_incidencies import render_pestanya_incidencies
from ui_planificacio_cp_sat import render_pestanya_planificacio_cp_sat


BASE_DIR = Path(__file__).resolve().parent
DEMO_DATABASE_PATH = BASE_DIR / "data" / "treballadors_demo.db"

st.set_page_config(
    page_title="Planificador de cobertures",
    page_icon=":material/calendar_month:",
    layout="wide",
)


def _session_database_path() -> Path:
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

st.title("Planificador de cobertures")
st.caption(
    "Crea el pla, consulta la disponibilitat i resol incidències des d'un "
    "únic lloc."
)

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
            "Falta `data/treballadors_demo.db`.",
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

if DATABASE_PATH.exists():
    planning_tab, query_tab, incidents_tab = st.tabs(
        [
            "Planificació",
            "Consulta",
            "Incidències",
        ]
    )
    with planning_tab:
        render_pestanya_planificacio_cp_sat(DATABASE_PATH)
    with query_tab:
        render_pestanya_descansos(DATABASE_PATH)
    with incidents_tab:
        render_pestanya_incidencies(DATABASE_PATH)
else:
    st.error(
        "Afegeix una base pseudonimitzada a `data/treballadors_demo.db` abans "
        "de publicar l'aplicació.",
        icon=":material/database_off:",
    )
