"""Pàgina de planificació incremental."""

from pathlib import Path

import streamlit as st

from planificador_cp_sat.ui.planificacio import render_pestanya_planificacio_cp_sat


render_pestanya_planificacio_cp_sat(Path(st.session_state["database_path"]))
