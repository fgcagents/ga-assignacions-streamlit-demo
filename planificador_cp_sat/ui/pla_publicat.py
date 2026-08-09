"""Pestanya de consulta del pla oficial desat a SQLite."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from planificador_cp_sat.services.pla_publicat import (
    PublishedPlanFilters,
    PublishedPlanReadError,
    list_published_assignments,
    list_published_plan_versions,
    load_published_assignment_detail,
    load_published_plan_filter_options,
    load_published_plan_summary,
)


_STATE_LABELS = {
    "publicada": "Publicada",
    "bloquejada": "Bloquejada",
}
_EVENT_LABELS = {
    "bootstrap": "Estat inicial importat",
    "publicacio": "Publicació",
    "rollback": "Reversió",
}


def _as_date(value: object) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _date_label(value: object) -> str:
    parsed = _as_date(value)
    return parsed.strftime("%d/%m/%Y") if parsed else "—"


def _datetime_label(value: object) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return str(value)


def _selected_range(value: object) -> tuple[date | None, date | None]:
    if isinstance(value, (tuple, list)):
        if len(value) == 2:
            return _as_date(value[0]), _as_date(value[1])
        if len(value) == 1:
            selected = _as_date(value[0])
            return selected, selected
    selected = _as_date(value)
    return selected, selected


def _render_summary(summary: dict) -> None:
    event = _EVENT_LABELS.get(summary["event_type"], summary["event_type"])
    st.badge(
        f"Versió oficial V{summary['version']} · {event}",
        icon=":material/verified:",
        color="green",
    )
    st.caption(
        f"Vigent des de {_datetime_label(summary['published_at'])}. "
        "Aquesta vista només mostra dades oficials desades a la base de dades."
    )
    st.markdown(
        f"**{_date_label(summary['start_date'])} – "
        f"{_date_label(summary['end_date'])}**  \n"
        f"{summary['assignments']} assignacions actives · "
        f"{summary['workers']} treballadors · {summary['services']} serveis"
    )
    if summary["cancelled"]:
        st.caption(
            "Les assignacions anul·lades es conserven per traçabilitat i "
            "no formen part del pla actiu."
        )
    if summary["blocked"]:
        st.info(
            f"Hi ha {summary['blocked']} assignació/ns bloquejada/es dins del pla oficial.",
            icon=":material/lock:",
        )


def _render_filters(summary: dict, options: dict) -> PublishedPlanFilters:
    worker_labels = {
        worker_id: f"{label} · {worker_id}"
        for worker_id, label in options["workers"]
    }
    start = _as_date(summary["start_date"]) or date.today()
    end = _as_date(summary["end_date"]) or start
    with st.form("filtres_pla_publicat", border=True):
        st.subheader("Filtres")
        first, second = st.columns(2)
        selected_dates = first.date_input(
            "Rang de dates",
            value=(start, end),
            format="DD/MM/YYYY",
            key="pla_publicat_dates",
        )
        selected_workers = second.multiselect(
            "Treballador",
            options=list(worker_labels),
            format_func=lambda value: worker_labels[value],
            key="pla_publicat_treballadors",
        )
        third, fourth = st.columns(2)
        selected_lines = third.multiselect(
            "Línia",
            options=options["lines"],
            key="pla_publicat_linies",
        )
        selected_services = fourth.multiselect(
            "Servei",
            options=options["services"],
            key="pla_publicat_serveis",
        )
        apply_filter, clear_filter = st.columns(2)
        apply_filter.form_submit_button(
            "Aplicar filtres",
            icon=":material/filter_alt:",
            width="stretch",
            type="primary",
        )
        clear = clear_filter.form_submit_button(
            "Netejar filtres",
            icon=":material/filter_alt_off:",
            width="stretch",
        )
    if clear:
        for key in (
            "pla_publicat_dates",
            "pla_publicat_treballadors",
            "pla_publicat_linies",
            "pla_publicat_serveis",
            "pla_publicat_taula",
        ):
            st.session_state.pop(key, None)
        st.rerun()
    selected_start, selected_end = _selected_range(selected_dates)
    return PublishedPlanFilters(
        start_date=selected_start,
        end_date=selected_end,
        worker_ids=tuple(selected_workers),
        lines=tuple(selected_lines),
        services=tuple(selected_services),
    )


def _display_frame(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    display = pd.DataFrame(
        {
            "Data": pd.to_datetime(frame["data"], errors="coerce").dt.date,
            "Treballador": frame["worker_name"],
            "Servei": frame["service"],
            "Inici": frame["start_time"],
            "Final": frame["end_time"],
            "Línia": frame["line"],
            "Zona": frame["zone"],
            "Torn": frame["shift"].replace("", "—"),
            "Estat": frame["assignment_state"].map(_STATE_LABELS),
            "Publicació": pd.to_datetime(
                frame["published_at"], errors="coerce"
            ),
            "Versió": frame["plan_version"].map(lambda item: f"V{item}"),
            "Avisos": frame.apply(
                lambda item: " · ".join(
                    part
                    for part in (
                        (
                            f"{int(item['incident_count'])} incidència/es"
                            if item["incident_count"]
                            else ""
                        ),
                        (
                            f"{int(item['change_count'])} canvi/s"
                            if item["change_count"]
                            else ""
                        ),
                    )
                    if part
                )
                or "—",
                axis=1,
            ),
        }
    )
    return display


def _render_assignment_detail(database_path: str | Path, row: dict) -> None:
    detail = load_published_assignment_detail(database_path, int(row["id"]))
    assignment = detail["assignment"]
    st.subheader("Detall de l'assignació")
    st.caption(
        f"{assignment['worker_name']} · {_date_label(assignment['data'])} · "
        f"Servei {assignment['service']} · Assignació #{assignment['id']}"
    )
    if row.get("execution_id"):
        st.write(
            f"Procedència: execució #{row['execution_id']} · "
            f"{row.get('origen') or 'manual'}"
        )
    else:
        st.write("Procedència: estat inicial importat (V1)")

    incidents_tab, changes_tab, locks_tab = st.tabs(
        [
            f"Incidències ({len(detail['incidents'])})",
            f"Canvis ({len(detail['changes'])})",
            f"Bloquejos ({len(detail['locks'])})",
        ]
    )
    with incidents_tab:
        if detail["incidents"]:
            st.dataframe(
                pd.DataFrame(detail["incidents"]),
                hide_index=True,
                width="stretch",
            )
        else:
            st.caption("No hi ha incidències relacionades amb aquesta data.")
    with changes_tab:
        if detail["changes"]:
            st.dataframe(
                pd.DataFrame(detail["changes"]),
                hide_index=True,
                width="stretch",
            )
        else:
            st.caption("No hi ha canvis incrementals registrats.")
    with locks_tab:
        if detail["locks"]:
            st.dataframe(
                pd.DataFrame(detail["locks"]),
                hide_index=True,
                width="stretch",
            )
        else:
            st.caption("No hi ha bloquejos relacionats.")


def _render_versions(database_path: str | Path) -> None:
    with st.expander("Historial de versions oficials"):
        versions = list_published_plan_versions(database_path)
        frame = pd.DataFrame(versions)
        if frame.empty:
            st.caption("Encara no hi ha versions registrades.")
            return
        frame["versio"] = frame["versio"].map(lambda item: f"V{item}")
        frame["tipus_event"] = frame["tipus_event"].map(
            lambda item: _EVENT_LABELS.get(item, item)
        )
        st.dataframe(frame, hide_index=True, width="stretch")


def render_pestanya_pla_publicat(database_path: str | Path) -> None:
    """Renderitza una vista oficial, filtrable i sense accions d'escriptura."""
    st.header("Pla publicat")
    st.caption(
        "Consulta la versió oficial desada a la base de dades i la seva "
        "traçabilitat. Els resultats provisionals no apareixen aquí."
    )
    try:
        summary = load_published_plan_summary(database_path)
        options = load_published_plan_filter_options(database_path)
        _render_summary(summary)
        filters = _render_filters(summary, options)
        rows = list_published_assignments(database_path, filters)
        st.subheader(f"Assignacions oficials ({len(rows)})")
        if not rows:
            st.info(
                "No hi ha assignacions que coincideixin amb els filtres.",
                icon=":material/search_off:",
            )
            _render_versions(database_path)
            return
        frame = _display_frame(rows)
        event = st.dataframe(
            frame,
            hide_index=True,
            width="stretch",
            on_select="rerun",
            selection_mode="single-row",
            key="pla_publicat_taula",
            column_config={
                "Data": st.column_config.DateColumn(format="DD/MM/YYYY"),
                "Publicació": st.column_config.DatetimeColumn(
                    format="DD/MM/YYYY HH:mm",
                    help=(
                        "Data de publicació. Per a les assignacions inicials "
                        "V1, mostra la data d'incorporació registrada."
                    ),
                ),
            },
        )
        st.download_button(
            "Descarregar resultats (CSV)",
            data=frame.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"pla_publicat_V{summary['version']}.csv",
            mime="text/csv",
            icon=":material/download:",
        )
        selected_rows = event.selection.rows
        if selected_rows:
            _render_assignment_detail(database_path, rows[selected_rows[0]])
        else:
            st.caption(
                "Selecciona una fila per veure incidències, canvis i bloquejos."
            )
        _render_versions(database_path)
    except PublishedPlanReadError as error:
        st.error(str(error), icon=":material/database_off:")
