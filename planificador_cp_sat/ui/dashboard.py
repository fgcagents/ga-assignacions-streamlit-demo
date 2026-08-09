"""Pàgina inicial amb el resum operatiu del planificador."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

import streamlit as st

from planificador_cp_sat.services.dashboard import load_dashboard_summary


def _date_label(value: object, *, include_year: bool = True) -> str:
    if not value:
        return "—"
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value)
    return parsed.strftime("%d/%m/%Y" if include_year else "%d/%m")


def _datetime_label(value: object) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return parsed.strftime("%d/%m/%Y %H:%M")


def _render_kpis(summary: dict[str, Any]) -> None:
    reference = summary["reference_date"]
    needs = summary["coverage_needs"]
    covered = summary["coverage_covered"]
    coverage_value = f"{covered / needs:.1%}" if needs else "—"
    if reference == date.today().isoformat():
        coverage_label = "Cobertura avui"
    elif reference:
        coverage_label = f"Cobertura {_date_label(reference, include_year=False)}"
    else:
        coverage_label = "Cobertura"
    with st.container(horizontal=True):
        st.metric(
            coverage_label,
            coverage_value,
            f"{covered}/{needs} serveis coberts" if needs else "Sense necessitats",
            delta_color="off",
            border=True,
            help="Cobertura del pla oficial a la data de referència.",
        )
        st.metric(
            "Descoberts 7 dies",
            summary["uncovered_next_7"],
            "Requereixen planificació" if summary["uncovered_next_7"] else None,
            delta_color="inverse",
            border=True,
            help=(
                "Necessitats sense assignació oficial entre la data de "
                "referència i els sis dies següents."
            ),
        )
        st.metric(
            "Incidències obertes",
            summary["open_incidents"],
            border=True,
            help="Incidències registrades o amb una reparació en preparació.",
        )
        st.metric(
            "Propostes pendents",
            summary["pending_total"],
            border=True,
            help="Propostes de planificació i reparacions encara no aplicades.",
        )


def _render_attention(summary: dict[str, Any]) -> None:
    st.subheader("Requereix atenció")
    has_attention = False
    if summary["uncovered_next_7"]:
        has_attention = True
        affected_dates = len(summary["uncovered_by_date"])
        st.markdown(
            f":material/event_busy: **{summary['uncovered_next_7']} serveis "
            f"sense cobertura** en {affected_dates} dia/dies del període immediat."
        )
        dates = ", ".join(
            f"{_date_label(item['date'], include_year=False)} ({item['count']})"
            for item in summary["uncovered_by_date"]
        )
        st.caption(dates)
    if summary["ending_leaves"]:
        has_attention = True
        st.markdown(
            f":material/sick: **{len(summary['ending_leaves'])} baixa/baixes "
            "finalitzen durant els pròxims 7 dies.**"
        )
    if summary["open_incidents"]:
        has_attention = True
        st.markdown(
            f":material/report: **{summary['open_incidents']} incidència/es "
            "encara estan obertes.**"
        )
    if summary["pending_total"]:
        has_attention = True
        parts = []
        if summary["pending_planning"]:
            parts.append(f"{summary['pending_planning']} de planificació")
        if summary["pending_repairs"]:
            parts.append(f"{summary['pending_repairs']} de reparació")
        st.markdown(
            ":material/pending_actions: **Hi ha propostes pendents:** "
            + " i ".join(parts)
            + "."
        )
    if not has_attention:
        st.success(
            "No hi ha elements pendents en el període immediat.",
            icon=":material/check_circle:",
        )


def _render_quick_actions() -> None:
    st.subheader("Accessos ràpids")
    first, second = st.columns(2)
    if first.button(
        "Registrar incidència",
        icon=":material/add_alert:",
        type="primary",
        width="stretch",
    ):
        st.switch_page("app_pages/incidencies.py")
    if second.button(
        "Generar proposta",
        icon=":material/auto_awesome:",
        width="stretch",
    ):
        st.switch_page("app_pages/planificacio.py")
    third, fourth = st.columns(2)
    if third.button(
        "Consultar pla publicat",
        icon=":material/fact_check:",
        width="stretch",
    ):
        st.switch_page("app_pages/pla_publicat.py")
    if fourth.button(
        "Consultar treballador",
        icon=":material/person_search:",
        width="stretch",
    ):
        st.switch_page("app_pages/personal.py")


def _render_official_plan(plan: dict[str, Any] | None) -> None:
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.subheader("Pla oficial")
            if plan:
                st.badge(
                    f"Versió V{plan['version']}",
                    icon=":material/verified:",
                    color="green",
                )
        if not plan:
            st.info("Encara no hi ha cap versió oficial disponible.")
            return
        st.markdown(
            f"**{_date_label(plan['start_date'])} – "
            f"{_date_label(plan['end_date'])}**\n\n"
            f"{plan['assignments']} assignacions actives · "
            f"{plan['workers']} treballadors · {plan['services']} serveis"
        )
        notes = []
        if plan["blocked"]:
            notes.append(f"{plan['blocked']} bloquejades")
        if plan["cancelled"]:
            notes.append(f"{plan['cancelled']} anul·lades conservades")
        published = f"Actualitzat {_datetime_label(plan['published_at'])}"
        if notes:
            published += " · " + " · ".join(notes)
        st.caption(published)


def _render_recent_activity(activity: list[dict[str, str]]) -> None:
    with st.container(border=True):
        st.subheader("Activitat recent")
        if not activity:
            st.caption("Encara no hi ha activitat registrada.")
            return
        for item in activity:
            st.markdown(f"**{item['label']}**")
            st.caption(_datetime_label(item["created_at"]))


def render_dashboard(database_path: str | Path) -> None:
    """Renderitza la portada operativa de l'aplicació."""
    st.header("Resum operatiu")
    st.caption(
        "Estat del pla, avisos prioritaris i accessos directes per començar."
    )
    try:
        summary = load_dashboard_summary(database_path)
    except (OSError, sqlite3.Error, ValueError) as error:
        st.error(
            f"No s'ha pogut preparar el resum: {error}",
            icon=":material/database_off:",
        )
        return

    if summary["reference_date"] is None:
        st.warning(
            "No hi ha cap període de cobertura disponible per resumir.",
            icon=":material/event_busy:",
        )
    elif summary["reference_date"] != date.today().isoformat():
        st.info(
            "La data actual queda fora del pla disponible. El resum mostra "
            f"la situació a {_date_label(summary['reference_date'])}.",
            icon=":material/history:",
        )

    _render_kpis(summary)
    attention, actions = st.columns([3, 2])
    with attention.container(border=True, height="stretch"):
        _render_attention(summary)
    with actions.container(border=True, height="stretch"):
        _render_quick_actions()

    plan_column, activity_column = st.columns([3, 2])
    with plan_column:
        _render_official_plan(summary["plan"])
    with activity_column:
        _render_recent_activity(summary["recent_activity"])
