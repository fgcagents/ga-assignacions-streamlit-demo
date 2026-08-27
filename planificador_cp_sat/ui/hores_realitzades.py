"""Pantalla de tancament massiu i consulta d'hores realitzades."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from planificador_cp_sat.services.hores_realitzades import (
    RealizedHoursError,
    close_period,
    coverage_date_limits,
    list_closures,
    list_realized_hours,
    preview_period_closure,
    realized_hours_by_worker,
    reverse_closure,
)


PREVIEW_KEY = "hores_realitzades_previsualitzacio"


def _hours(minutes: object) -> str:
    return f"{int(minutes or 0) / 60:.2f} h"


def _render_closure(db_path: str, limits: tuple[date, date]) -> None:
    st.subheader("Tancar una finestra de dies")
    st.caption(
        "Confirma com a realitzades les assignacions publicades o bloquejades. "
        "Les que ja estan confirmades no es tornen a sumar."
    )
    default_end = min(limits[1], limits[0] + timedelta(days=6))
    start_col, end_col = st.columns(2)
    with start_col:
        start_day = st.date_input(
            "Data inicial",
            value=limits[0],
            min_value=limits[0],
            max_value=limits[1],
            key="hores_tancament_inici",
        )
    with end_col:
        end_day = st.date_input(
            "Data final",
            value=default_end,
            min_value=limits[0],
            max_value=limits[1],
            key="hores_tancament_fi",
        )

    if st.button(
        "Revisar el tancament",
        icon=":material/search:",
        type="primary",
        key="hores_revisar_tancament",
    ):
        try:
            st.session_state[PREVIEW_KEY] = preview_period_closure(
                db_path, start_day, end_day
            )
        except RealizedHoursError as error:
            st.error(str(error))

    preview = st.session_state.get(PREVIEW_KEY)
    if not preview:
        st.info("Escull el període i revisa quines assignacions es confirmaran.")
        return
    if (
        preview["data_inici"] != start_day.isoformat()
        or preview["data_fi"] != end_day.isoformat()
    ):
        st.warning("Has canviat les dates. Torna a revisar el període.")
        return

    metric_cols = st.columns(4)
    metric_cols[0].metric("Publicades", preview["publicades"], border=True)
    metric_cols[1].metric("Pendents", preview["pendents"], border=True)
    metric_cols[2].metric(
        "Ja confirmades", preview["ja_confirmades"], border=True
    )
    metric_cols[3].metric(
        "Hores noves", _hours(preview["minuts_pendents"]), border=True
    )

    rows = preview["assignacions"]
    if rows:
        frame = pd.DataFrame(rows)
        frame["estat"] = frame["ja_confirmada"].map(
            {True: "Ja confirmada", False: "Es confirmarà"}
        )
        frame["hores"] = frame["durada_minuts"].map(
            lambda value: round(value / 60, 2)
        )
        st.dataframe(
            frame[
                [
                    "data",
                    "servei",
                    "treballador",
                    "hora_inici",
                    "hora_fi",
                    "hores",
                    "estat",
                ]
            ],
            width="stretch",
            hide_index=True,
            column_config={
                "data": "Data",
                "servei": "Servei",
                "treballador": "Treballador",
                "hora_inici": "Inici",
                "hora_fi": "Final",
                "hores": "Hores",
                "estat": "Resultat",
            },
        )
    else:
        st.warning("No hi ha assignacions publicades en aquest període.")

    if not preview["pendents"]:
        st.success("Aquest període ja està completament confirmat.")
        return
    confirmed = st.checkbox(
        "He revisat el període i vull confirmar totes les assignacions pendents.",
        key="hores_confirma_tancament",
    )
    if st.button(
        f"Tancar {preview['pendents']} assignacions",
        icon=":material/lock_clock:",
        type="primary",
        disabled=not confirmed,
        key="hores_executa_tancament",
    ):
        try:
            result = close_period(
                db_path,
                start_day,
                end_day,
                expected_fingerprint=str(preview["empremta"]),
            )
        except RealizedHoursError as error:
            st.error(str(error))
        else:
            st.session_state.pop(PREVIEW_KEY, None)
            st.success(
                f"Tancament #{result['tancament_id']} creat: "
                f"{result['confirmades_ara']} assignacions i "
                f"{_hours(result['minuts_confirmats_ara'])}."
            )
            st.rerun()


def _render_tracking(db_path: str, limits: tuple[date, date]) -> None:
    st.subheader("Totals confirmats")
    years = list(range(limits[0].year, limits[1].year + 1))
    selected_year = st.selectbox(
        "Any",
        years,
        index=len(years) - 1,
        key="hores_any_consulta",
    )
    totals = pd.DataFrame(realized_hours_by_worker(db_path, year=selected_year))
    if totals.empty:
        st.info("No hi ha treballadors del grup T per mostrar.")
    else:
        st.dataframe(
            totals[
                [
                    "treballador_id",
                    "treballador",
                    "hores_realitzades",
                    "canvis_torn",
                    "percentatge_canvis_torn",
                    "canvis_zona",
                    "percentatge_canvis_zona",
                    "serveis_confirmats",
                ]
            ],
            width="stretch",
            hide_index=True,
            column_config={
                "treballador_id": "ID",
                "treballador": "Treballador",
                "hores_realitzades": "Hores realitzades",
                "canvis_torn": "Canvis Torn",
                "percentatge_canvis_torn": st.column_config.NumberColumn(
                    "% Canvis Torn", format="%.1f %%"
                ),
                "canvis_zona": "Canvis Zona",
                "percentatge_canvis_zona": st.column_config.NumberColumn(
                    "% Canvis Zona", format="%.1f %%"
                ),
                "serveis_confirmats": "Serveis confirmats",
            },
        )

    with st.expander("Detall d'hores confirmades"):
        entries = pd.DataFrame(list_realized_hours(db_path, year=selected_year))
        if entries.empty:
            st.caption("Encara no hi ha hores confirmades aquest any.")
        else:
            entries["hores"] = entries["durada_minuts"].map(
                lambda value: round(value / 60, 2)
            )
            st.dataframe(
                entries[
                    [
                        "data",
                        "servei",
                        "treballador",
                        "hora_inici",
                        "hora_fi",
                        "hores",
                        "tancament_id",
                    ]
                ],
                width="stretch",
                hide_index=True,
                column_config={
                    "data": "Data",
                    "servei": "Servei",
                    "treballador": "Treballador",
                    "hora_inici": "Inici",
                    "hora_fi": "Final",
                    "hores": "Hores",
                    "tancament_id": "Tancament",
                },
            )

    st.subheader("Historial de tancaments")
    closures = list_closures(db_path)
    if not closures:
        st.info("Encara no s'ha fet cap tancament.")
        return
    display = pd.DataFrame(closures)
    display["hores"] = display["minuts_confirmats"].map(
        lambda value: round(value / 60, 2)
    )
    st.dataframe(
        display[
            [
                "id",
                "data_inici",
                "data_fi",
                "estat",
                "assignacions_confirmades",
                "hores",
                "created_at",
            ]
        ],
        width="stretch",
        hide_index=True,
        column_config={
            "id": "Tancament",
            "data_inici": "Inici",
            "data_fi": "Final",
            "estat": "Estat",
            "assignacions_confirmades": "Assignacions",
            "hores": "Hores",
            "created_at": "Creat",
        },
    )

    active = [item for item in closures if item["estat"] == "tancat"]
    if not active:
        return
    with st.expander("Corregir un tancament"):
        selected = st.selectbox(
            "Tancament a revertir",
            active,
            format_func=lambda item: (
                f"#{item['id']} · {item['data_inici']} – {item['data_fi']} "
                f"· {item['assignacions_confirmades']} assignacions"
            ),
            key="hores_tancament_reversio",
        )
        reason = st.text_input(
            "Motiu de la correcció",
            key="hores_motiu_reversio",
        )
        confirmed = st.checkbox(
            "Entenc que aquestes hores deixaran de comptar com a realitzades.",
            key="hores_confirma_reversio",
        )
        if st.button(
            "Revertir el tancament",
            icon=":material/undo:",
            disabled=not confirmed or not reason.strip(),
            key="hores_executa_reversio",
        ):
            try:
                result = reverse_closure(
                    db_path, int(selected["id"]), reason=reason
                )
            except RealizedHoursError as error:
                st.error(str(error))
            else:
                st.success(
                    f"Tancament revertit. {result['hores_anul_lades']} "
                    "assignacions han deixat de comptar."
                )
                st.rerun()


def render_pestanya_hores_realitzades(db_path: str | Path) -> None:
    """Renderitza el flux principal de confirmació i consulta."""
    database = str(db_path)
    st.header("Hores realitzades")
    st.caption(
        "Tanca períodes de prova, consulta els totals reals del grup T i "
        "corregeix un tancament mantenint-ne l'historial."
    )
    limits = coverage_date_limits(database)
    if limits is None:
        st.warning("La base no conté cap període de cobertura.")
        return
    mode = st.segmented_control(
        "Acció",
        ["Tancar període", "Consultar i corregir"],
        default="Tancar període",
        key="hores_mode",
    )
    if mode == "Consultar i corregir":
        _render_tracking(database, limits)
    else:
        _render_closure(database, limits)
