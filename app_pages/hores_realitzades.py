"""Pàgina de tancament i consulta d'hores realitzades."""

from pathlib import Path

import streamlit as st

from planificador_cp_sat.ui.hores_realitzades import (
    render_pestanya_hores_realitzades,
)


render_pestanya_hores_realitzades(Path(st.session_state["database_path"]))
