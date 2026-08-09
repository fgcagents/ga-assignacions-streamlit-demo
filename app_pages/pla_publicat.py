"""Pàgina de consulta del pla oficial."""

from pathlib import Path

import streamlit as st

from planificador_cp_sat.ui.pla_publicat import render_pestanya_pla_publicat


render_pestanya_pla_publicat(Path(st.session_state["database_path"]))
