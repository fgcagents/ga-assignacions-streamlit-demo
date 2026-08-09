"""Pàgina de gestió d'incidències."""

from pathlib import Path

import streamlit as st

from planificador_cp_sat.ui.incidencies import render_pestanya_incidencies


render_pestanya_incidencies(Path(st.session_state["database_path"]))
