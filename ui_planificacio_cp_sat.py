"""Interfície Streamlit per generar una primera cobertura amb CP-SAT."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from servei_planificacio_cp_sat import (
    discard_initial_coverage_draft,
    initialize_cp_sat_drafts,
    limits_cobertura,
    list_initial_coverage_drafts,
    load_initial_coverage_draft,
    publish_initial_coverage_draft,
    rollback_initial_coverage_publication,
    save_initial_coverage_draft,
    validate_initial_coverage_draft,
)


RESULT_KEY = "resultat_planificacio_inicial_cp_sat"


def _coverage_by_day(result: dict) -> pd.DataFrame:
    covered = pd.DataFrame(result["assignacions"])
    uncovered = pd.DataFrame(result["descobertes"])
    pieces = []
    if not covered.empty:
        daily = covered.groupby("data", as_index=False).size()
        daily["situacio"] = "Cobertes"
        pieces.append(daily)
    if not uncovered.empty:
        daily = uncovered.groupby("data", as_index=False).size()
        daily["situacio"] = "Descobertes"
        pieces.append(daily)
    if not pieces:
        return pd.DataFrame(columns=["data", "size", "situacio"])
    result_frame = pd.concat(pieces, ignore_index=True)
    result_frame["data"] = pd.to_datetime(result_frame["data"])
    return result_frame.rename(columns={"size": "necessitats"})


def _worker_workload(result: dict) -> pd.DataFrame:
    assignments = pd.DataFrame(result["assignacions"])
    if assignments.empty:
        return pd.DataFrame(
            columns=["treballador", "hores", "assignacions"]
        )
    return (
        assignments.groupby("treballador", as_index=False)
        .agg(
            hores=("durada_hores", "sum"),
            assignacions=("necessitat_id", "count"),
        )
        .sort_values(["hores", "treballador"], ascending=[False, True])
    )


def _render_functional_validation(validation: dict) -> None:
    ge = validation["serveis_ge"]
    zero = validation["horaris_0"]
    rest = validation["descans_12h"]
    load = validation["carrega"]
    errors = validation["solver"]["errors_validador"]

    if errors:
        st.error(
            f"El validador ha detectat {len(errors)} incidència(es) dures."
        )
        st.dataframe(pd.DataFrame({"Error": errors}), hide_index=True)
    else:
        st.success("La proposta no conté vulneracions de les regles dures.")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric(
        "Serveis GE",
        f"{ge['cobertes']}/{ge['necessitats']}",
        f"{ge['descobertes']} descoberts",
        delta_color="off",
        border=True,
    )
    col2.metric(
        "Horaris 0",
        f"{zero['cobertes']}/{zero['necessitats']}",
        f"{zero['descobertes']} descoberts",
        delta_color="off",
        border=True,
    )
    minimum_rest = rest["descans_minim_observat_hores"]
    col3.metric(
        "Descans mínim observat",
        f"{minimum_rest:.2f} h" if minimum_rest is not None else "—",
        f"{rest['violacions']} vulneracions",
        delta_color="off",
        border=True,
    )
    col4.metric(
        "Límit anual",
        f"{len(load['sobrecarregats'])} sobrecarregats",
        f"{len(load['a_partir_90_percent_limit'])} al 90% o més",
        delta_color="off",
        border=True,
    )

    with st.expander("Disponibilitat per habilitació"):
        st.dataframe(
            pd.DataFrame(validation["perfils_habilitacio"]).rename(
                columns={
                    "perfil": "Perfil",
                    "necessitats": "Necessitats",
                    "descobertes": "Descobertes",
                    "treballadors_amb_habilitacio": "Treballadors habilitats",
                    "candidats_estatics_minim": "Mínim candidats",
                    "candidats_estatics_mitjana": "Mitjana candidats",
                    "candidats_estatics_maxim": "Màxim candidats",
                }
            ),
            width="stretch",
            hide_index=True,
        )

    assigned = load["treballadors_assignats"]
    total_workers = load["treballadors_t"]
    annual_range = load["rang_hores_anuals_despres"]
    st.markdown("#### Càrrega de treball")
    st.write(
        f"{assigned}/{total_workers} treballadors T reben assignacions. "
        f"El rang anual resultant és de {annual_range:.2f} hores."
    )
    if load["sense_assignacions"]:
        st.warning(
            f"Hi ha {len(load['sense_assignacions'])} treballador(s) "
            "sense cap assignació en aquest període."
        )
        st.dataframe(
            pd.DataFrame(load["sense_assignacions"])[
                [
                    "treballador",
                    "hores_anuals_despres",
                    "candidatures_estatiques",
                ]
            ].rename(
                columns={
                    "treballador": "Treballador",
                    "hores_anuals_despres": "Hores anuals resultants",
                    "candidatures_estatiques": "Candidatures possibles",
                }
            ),
            width="stretch",
            hide_index=True,
        )


def _render_review_actions(db_path: str | Path, result: dict) -> None:
    draft_id = result.get("esborrany_id")
    if draft_id is None:
        return
    state = result.get("estat_esborrany", "esborrany")
    state_labels = {
        "esborrany": ("Pendent de revisió", "orange"),
        "validada": ("Validada", "blue"),
        "publicada": ("Publicada", "green"),
        "descartada": ("Descartada", "red"),
    }
    label, color = state_labels.get(state, (state.capitalize(), "gray"))
    st.badge(label, color=color)
    if state == "publicada":
        st.success(
            "Proposta publicada atòmicament al pla operatiu."
        )
        publication = result.get("publicacio") or {}
        if publication.get("backup_path"):
            st.caption(
                "Còpia de seguretat prèvia: "
                f"{publication['backup_path']}"
            )
        st.warning(
            "El rollback retira aquesta publicació i restaura exactament "
            "les assignacions i l'històric anteriors. Es bloquejarà si "
            "detecta canvis posteriors al mateix període."
        )
        confirm_rollback = st.checkbox(
            "Confirmo que vull revertir aquesta publicació",
            key=f"cp_sat_rollback_confirm_{draft_id}",
        )
        if st.button(
            "Fer rollback de la publicació",
            icon=":material/undo:",
            width="stretch",
            disabled=not confirm_rollback,
            key=f"cp_sat_rollback_{draft_id}",
        ):
            try:
                rollback_initial_coverage_publication(
                    db_path, int(draft_id)
                )
                st.session_state[RESULT_KEY] = load_initial_coverage_draft(
                    db_path, int(draft_id)
                )
                st.session_state["cp_sat_notice"] = (
                    f"S'ha revertit la publicació de la proposta #{draft_id}."
                )
                st.rerun()
            except (sqlite3.Error, ValueError) as error:
                st.error(str(error))
        return
    elif state == "descartada":
        st.warning("Aquesta proposta està descartada i no es pot validar.")
        return
    elif state == "validada":
        st.success(
            "Proposta validada i preparada per a la publicació final."
        )
    else:
        st.info(
            "Revisa assignacions, descoberts, validació funcional i "
            "comprovacions funcionals abans de confirmar."
        )

    if state == "esborrany":
        confirm_review = st.checkbox(
            "Confirmo que he revisat la proposta",
            key=f"cp_sat_confirm_review_{draft_id}",
        )
        if st.button(
            "Validar proposta",
            type="primary",
            icon=":material/check_circle:",
            width="stretch",
            disabled=not confirm_review,
            key=f"cp_sat_validate_{draft_id}",
        ):
            try:
                validate_initial_coverage_draft(db_path, int(draft_id))
                result["estat_esborrany"] = "validada"
                st.session_state["cp_sat_notice"] = (
                    f"La proposta #{draft_id} ha quedat validada."
                )
                st.rerun()
            except (sqlite3.Error, ValueError) as error:
                st.error(str(error))

    if state == "validada":
        st.warning(
            "Publicar substituirà el pla actiu del període per aquesta "
            "proposta. Abans d'escriure, es repetiran totes les comprovacions "
            "i es crearà una còpia de seguretat."
        )
        confirm_publication = st.checkbox(
            "Confirmo que vull publicar tota la proposta",
            key=f"cp_sat_publish_confirm_{draft_id}",
        )
        if st.button(
            "Publicar proposta",
            type="primary",
            icon=":material/publish:",
            width="stretch",
            disabled=not confirm_publication,
            key=f"cp_sat_publish_{draft_id}",
        ):
            try:
                publication = publish_initial_coverage_draft(
                    db_path, int(draft_id)
                )
                st.session_state[RESULT_KEY] = load_initial_coverage_draft(
                    db_path, int(draft_id)
                )
                st.session_state["cp_sat_notice"] = (
                    f"Proposta #{draft_id} publicada: "
                    f"{publication['assignacions_publicades']} assignacions."
                )
                st.rerun()
            except (sqlite3.Error, ValueError) as error:
                st.error(str(error))

    if state in {"esborrany", "validada"}:
        confirm_discard = st.checkbox(
            "Confirmo que la vull descartar",
            key=f"cp_sat_review_discard_confirm_{draft_id}",
        )
        if st.button(
            "Descartar proposta",
            icon=":material/delete:",
            width="stretch",
            disabled=not confirm_discard,
            key=f"cp_sat_review_discard_{draft_id}",
        ):
            try:
                discard_initial_coverage_draft(db_path, int(draft_id))
                st.session_state.pop(RESULT_KEY, None)
                st.session_state["cp_sat_notice"] = (
                    f"La proposta #{draft_id} ha quedat descartada."
                )
                st.rerun()
            except (sqlite3.Error, ValueError) as error:
                st.error(str(error))


def _render_result(
    result: dict,
    db_path: str | Path | None = None,
) -> None:
    covered = int(result["necessitats_cobertes"])
    total = int(result["necessitats_totals"])
    uncovered = total - covered
    assignments = pd.DataFrame(result["assignacions"])
    workers = (
        assignments["treballador_id"].nunique()
        if not assignments.empty
        else 0
    )
    hours = assignments["durada_hores"].sum() if not assignments.empty else 0

    st.subheader("Proposta calculada")
    proposal_text = (
        f"Proposta #{result['esborrany_id']}"
        if result.get("esborrany_id")
        else "Proposta no desada"
    )
    proposal_state = result.get("estat_esborrany", "esborrany")
    state_labels = {
        "esborrany": "pendent de revisió",
        "validada": "validada",
        "publicada": "publicada",
        "descartada": "descartada",
    }
    plan_text = (
        "pla actualitzat"
        if proposal_state == "publicada"
        else "pla actual sense canvis"
    )
    st.caption(
        f"{proposal_text} · {result['data_inici']} → {result['data_fi']} · "
        f"{state_labels.get(proposal_state, proposal_state)} · {plan_text}"
    )
    col1, col2, col3, col4 = st.columns(4)
    col1.metric(
        "Cobertura",
        f"{covered}/{total}",
        f"{100 * covered / total:.1f}%" if total else None,
        border=True,
    )
    col2.metric("Serveis descoberts", uncovered, border=True)
    col3.metric("Treballadors assignats", workers, border=True)
    col4.metric("Hores assignades", f"{hours:.1f} h", border=True)

    if uncovered:
        st.warning(
            f"La proposta és vàlida, però queden {uncovered} serveis "
            "sense cobertura. Revisa la pestanya Descoberts."
        )
    else:
        st.success("Cobertura completa i validada pel model CP-SAT.")

    daily = _coverage_by_day(result)
    if not daily.empty:
        figure = px.bar(
            daily,
            x="data",
            y="necessitats",
            color="situacio",
            barmode="stack",
            color_discrete_map={
                "Cobertes": "#2ca02c",
                "Descobertes": "#d62728",
            },
        )
        figure.update_layout(
            height=330,
            margin={"l": 10, "r": 10, "t": 10, "b": 10},
            xaxis_title=None,
            yaxis_title="Necessitats",
            legend_title_text="Situació",
        )
        st.plotly_chart(figure, width="stretch")

    (
        result_tab,
        uncovered_tab,
        validation_tab,
        decision_tab,
    ) = st.tabs(
        [
            "Assignacions",
            "Descoberts",
            "Comprovacions",
            "Decisió",
        ]
    )
    with result_tab:
        workload = _worker_workload(result)
        if not workload.empty:
            st.markdown("#### Càrrega assignada per treballador")
            workload_figure = px.bar(
                workload,
                x="hores",
                y="treballador",
                orientation="h",
                hover_data=["assignacions"],
                color="hores",
                color_continuous_scale="Blues",
            )
            workload_figure.update_layout(
                height=min(800, max(330, 28 * len(workload))),
                margin={"l": 10, "r": 10, "t": 10, "b": 10},
                xaxis_title="Hores",
                yaxis_title=None,
                coloraxis_showscale=False,
            )
            st.plotly_chart(workload_figure, width="stretch")
        st.dataframe(
            assignments,
            width="stretch",
            hide_index=True,
            column_config={
                "data": st.column_config.DateColumn("Data", format="DD/MM/YYYY"),
                "durada_hores": st.column_config.NumberColumn(
                    "Durada", format="%.2f h"
                ),
            },
        )
        st.download_button(
            "Descarregar proposta CSV",
            assignments.to_csv(index=False).encode("utf-8-sig"),
            file_name=(
                f"proposta_cp_sat_{result['data_inici']}_"
                f"{result['data_fi']}.csv"
            ),
            mime="text/csv",
            icon=":material/download:",
            width="stretch",
        )
    with uncovered_tab:
        if result["descobertes"]:
            uncovered_frame = pd.DataFrame(result["descobertes"]).drop(
                columns=["diagnostic_descobert"],
                errors="ignore",
            )
            st.dataframe(
                uncovered_frame,
                width="stretch",
                hide_index=True,
            )
        else:
            st.success("No hi ha serveis descoberts.")
    with validation_tab:
        validation = result.get("validacio_funcional")
        if validation:
            _render_functional_validation(validation)
        else:
            st.info(
                "Aquest esborrany és anterior al diagnòstic funcional. "
                "Les propostes noves ja el conservaran."
            )
    with decision_tab:
        if db_path is None:
            st.caption("La decisió només està disponible per a esborranys desats.")
        else:
            _render_review_actions(db_path, result)

    with st.expander(
        "Detalls tècnics de l'execució",
        icon=":material/settings:",
    ):
        st.write(
            {
                "Estat del solver": result["estat"],
                "Temps total": f"{result['temps_total_segons']:.2f} s",
                "Llavor seleccionada": result["llavor_seleccionada"],
                "Execucions": len(result["candidats_multillavor"]),
                "Aturada després de la primera llavor": result[
                    "aturada_primera_llavor"
                ],
            }
        )
        phases = pd.DataFrame(result["fases"])
        if not phases.empty:
            st.dataframe(phases, width="stretch", hide_index=True)


def render_pestanya_planificacio_cp_sat(db_path: str | Path) -> None:
    st.header("Planificació inicial")
    st.caption(
        "Genera una proposta, revisa-la i decideix si s'ha de publicar. "
        "Crear un esborrany no modifica el pla actual."
    )
    notice = st.session_state.pop("cp_sat_notice", None)
    if notice:
        st.success(notice)

    try:
        initialize_cp_sat_drafts(db_path)
        drafts = list_initial_coverage_drafts(db_path)
    except sqlite3.Error as error:
        st.error(f"No s'ha pogut preparar l'espai d'esborranys: {error}")
        return

    with st.expander(
        f"Propostes desades ({len(drafts)})",
        icon=":material/folder_open:",
        expanded=bool(drafts and not st.session_state.get(RESULT_KEY)),
    ):
        if not drafts:
            st.info("Encara no hi ha cap proposta CP-SAT desada.")
        else:
            draft_frame = pd.DataFrame(drafts)
            draft_frame = draft_frame.rename(
                columns={
                    "id": "Proposta",
                    "estat": "Estat",
                    "data_inici": "Inici",
                    "data_fi": "Final",
                    "necessitats_cobertes": "Cobertes",
                    "necessitats_totals": "Total",
                    "created_at": "Creada",
                }
            )
            st.dataframe(
                draft_frame[
                    [
                        "Proposta", "Estat", "Inici", "Final",
                        "Cobertes", "Total", "Creada",
                    ]
                ],
                width="stretch",
                hide_index=True,
                column_config={
                    "Inici": st.column_config.DateColumn(
                        "Inici", format="DD/MM/YYYY"
                    ),
                    "Final": st.column_config.DateColumn(
                        "Final", format="DD/MM/YYYY"
                    ),
                },
            )
            selected = st.selectbox(
                "Proposta per revisar",
                drafts,
                index=None,
                placeholder="Selecciona una proposta",
                format_func=lambda item: (
                    f"#{item['id']} · {item['estat']} · "
                    f"{item['data_inici']} → "
                    f"{item['data_fi']} · "
                    f"{item['necessitats_cobertes']}/"
                    f"{item['necessitats_totals']}"
                ),
                key="cp_sat_saved_draft",
            )
            if selected:
                open_col, discard_col = st.columns(2)
                with open_col:
                    if st.button(
                        "Obrir proposta",
                        icon=":material/visibility:",
                        width="stretch",
                        key="cp_sat_open_draft",
                    ):
                        st.session_state[RESULT_KEY] = (
                            load_initial_coverage_draft(
                                db_path, int(selected["id"])
                            )
                        )
                        st.rerun()
                with discard_col:
                    discard_confirmed = st.checkbox(
                        "Confirmo que el vull descartar",
                        key="cp_sat_discard_confirmed",
                        disabled=selected["estat"] == "publicada",
                    )
                    if st.button(
                        "Descartar proposta",
                        icon=":material/delete:",
                        width="stretch",
                        disabled=(
                            selected["estat"] == "publicada"
                            or not discard_confirmed
                        ),
                        key="cp_sat_discard_draft",
                    ):
                        discard_initial_coverage_draft(
                            db_path, int(selected["id"])
                        )
                        current = st.session_state.get(RESULT_KEY)
                        if current and current.get("esborrany_id") == int(
                            selected["id"]
                        ):
                            st.session_state.pop(RESULT_KEY, None)
                        st.rerun()

    st.subheader("Crear una proposta")

    try:
        minimum, maximum = limits_cobertura(db_path)
    except ValueError as error:
        st.error(str(error))
        return
    default_end = min(maximum, minimum + timedelta(days=30))
    with st.form("cp_sat_new_proposal"):
        col1, col2 = st.columns(2)
        with col1:
            start = st.date_input(
                "Data d'inici",
                value=minimum,
                min_value=minimum,
                max_value=maximum,
                key="cp_sat_initial_start",
            )
        with col2:
            end = st.date_input(
                "Data final",
                value=default_end,
                min_value=minimum,
                max_value=maximum,
                key="cp_sat_initial_end",
            )

        with st.expander(
            "Configuració avançada",
            icon=":material/tune:",
        ):
            col_time, col_equity, col_workers = st.columns(3)
            with col_time:
                time_limit = st.number_input(
                    "Temps per criteri (s)", 5, 300, 60, step=5
                )
            with col_equity:
                equity_time = st.number_input(
                    "Temps per a equitat (s)", 5, 60, 15, step=5
                )
            with col_workers:
                num_workers = st.number_input(
                    "Processos de càlcul", 1, 16, 8, step=1
                )
            force_seeds = st.checkbox(
                "Cercar alternatives addicionals",
                value=False,
                help=(
                    "Executa tres llavors encara que la primera ja demostri "
                    "la cobertura òptima."
                ),
            )

        generate = st.form_submit_button(
            "Generar proposta",
            type="primary",
            icon=":material/play_arrow:",
            width="stretch",
        )
    if generate:
        try:
            from cp_sat_pilot import SolverConfig
            from servei_planificacio_cp_sat import generate_initial_coverage

            with st.spinner(
                "CP-SAT està calculant cobertura, criteris operatius i "
                "equitat oportunista…"
            ):
                generated = generate_initial_coverage(
                    db_path,
                    start,
                    end,
                    config=SolverConfig(
                        max_time_seconds=float(time_limit),
                        equity_time_seconds=float(equity_time),
                        num_workers=int(num_workers),
                        random_seed=0,
                    ),
                    seeds=(0, 1, 2),
                    force_all_seeds=force_seeds,
                )
                draft_id = save_initial_coverage_draft(db_path, generated)
                generated["esborrany_id"] = draft_id
                generated["estat_esborrany"] = "esborrany"
                st.session_state[RESULT_KEY] = generated
            st.rerun()
        except ModuleNotFoundError as error:
            if error.name == "ortools":
                st.error(
                    "Falta OR-Tools. Executa `python -m pip install -e .` "
                    "dins de l'entorn virtual."
                )
            else:
                raise
        except ValueError as error:
            st.error(str(error))
        except sqlite3.Error as error:
            st.error(
                "La proposta s'ha calculat, però no s'ha pogut desar "
                f"l'esborrany: {error}"
            )

    result = st.session_state.get(RESULT_KEY)
    if result:
        if st.button(
            "Tancar proposta",
            icon=":material/close:",
            type="tertiary",
        ):
            st.session_state.pop(RESULT_KEY, None)
            st.rerun()
        _render_result(result, db_path=db_path)
