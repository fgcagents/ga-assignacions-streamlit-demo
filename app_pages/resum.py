"""Portada operativa."""

from pathlib import Path

import streamlit as st

from planificador_cp_sat.ui.dashboard import render_dashboard


render_dashboard(Path(st.session_state["database_path"]))
