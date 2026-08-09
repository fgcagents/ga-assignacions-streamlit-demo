"""Pàgina de consulta de personal i disponibilitat."""

from pathlib import Path

import streamlit as st

from planificador_cp_sat.ui.descansos import render_pestanya_descansos


render_pestanya_descansos(Path(st.session_state["database_path"]))
