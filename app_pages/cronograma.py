"""Pàgina informativa del cronograma de serveis."""

from pathlib import Path

import streamlit as st

from planificador_cp_sat.ui.cronograma import render_cronograma


render_cronograma(Path(st.session_state["database_path"]))
