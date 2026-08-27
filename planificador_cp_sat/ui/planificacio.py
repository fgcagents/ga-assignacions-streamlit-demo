"""Interfície Streamlit per revisar i publicar planificació incremental."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from planificador_cp_sat.services.auditoria_planificacio import (
    load_planning_publication_audit,
    rollback_planning_changeset,
)
from planificador_cp_sat.services.desplegament_planificacio import (
    load_planning_rollout_config,
    planning_shadow_report,
)
from planificador_cp_sat.services.esquema_planificacio import (
    migrate_planning_schema,
)
from planificador_cp_sat.services.persistencia_planificacio import (
    PlanningExecutionStaleError,
    StoredPlanningExecution,
    discard_planning_execution,
    list_planning_executions,
    load_planning_execution,
    save_planning_proposal,
    validate_planning_execution,
)
from planificador_cp_sat.services.publicacio_planificacio import (
    apply_planning_changeset,
)
from planificador_cp_sat.services.planificacio_selectiva import (
    SelectivePlanningError,
    load_selective_planning_options,
    preview_preassignments,
    revoke_preassignment,
    save_preassignments,
)
from planificador_cp_sat.solver_engines import (
    PUBLISHABLE_SOLVER_STATUSES,
    SolverEngine,
    normalize_solver_engine,
    solver_engine_label,
)


RESULT_KEY = "resultat_planificacio_inicial_cp_sat"
EXECUTION_ID_KEY = "planificacio_execucio_id"
NOTICE_KEY = "planificacio_notice"
STALE_KEY = "planificacio_obsoleta"
SELECTIVE_PREVIEW_KEY = "planificacio_selectiva_previsualitzacio"
WORKSPACE_VIEW_KEY = "planificacio_espai_treball"
WORKSPACE_VIEW_OVERRIDE_KEY = "planificacio_espai_treball_seguent"
SCOPE_REVIEW_KEY = "planificacio_abast_revisat"

VIEW_NEW_PROPOSAL = "Nova proposta"
VIEW_SAVED_PROPOSALS = "Revisar propostes"
VIEW_PREASSIGNMENTS = "Preassignacions"


def _coverage_limits(db_path: str | Path) -> tuple[date, date]:
    with sqlite3.connect(db_path) as connection:
        limits = connection.execute(
            "SELECT MIN(data), MAX(data) FROM cobertura"
        ).fetchone()
    if not limits or not limits[0] or not limits[1]:
        raise ValueError("No hi ha necessitats de cobertura disponibles")
    return date.fromisoformat(limits[0]), date.fromisoformat(limits[1])


def _data_llegible(value: str | date | None) -> str:
    if value is None:
        return "—"
    parsed = value if isinstance(value, date) else date.fromisoformat(value)
    return parsed.strftime("%d/%m/%Y")


def _render_current_plan_summary(summary: dict) -> None:
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.subheader("Pla vigent")
            if summary["assignacions_actives"]:
                st.badge(
                    "Actiu",
                    icon=":material/check_circle:",
                    color="green",
                )
        if not summary["assignacions_actives"]:
            st.info("Encara no hi ha cap planificació activa publicada.")
            return
        st.markdown(
            f"**{_data_llegible(summary['data_inici'])} – "
            f"{_data_llegible(summary['data_fi'])}**  \n"
            f"{summary['assignacions_actives']} assignacions actives · "
            f"{summary['publicacions_inicials_actives']} publicacions inicials "
            f"· {summary['reparacions_aplicades']} reparacions aplicades"
        )
        if summary["assignacions_bloquejades"]:
            st.caption(
                f"{summary['assignacions_bloquejades']} assignacions bloquejades."
            )
        metadata = []
        if summary.get("darrera_proposta_inicial_id") is not None:
            metadata.append(
                f"Darrera proposta inicial: "
                f"PI-{summary['darrera_proposta_inicial_id']}"
            )
        if summary.get("ultima_actualitzacio"):
            metadata.append(
                f"Darrera actualització: {summary['ultima_actualitzacio']}"
            )
        if metadata:
            st.caption(" · ".join(metadata))


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


def _render_current_plan_view(comparison: dict) -> None:
    repair_ids = comparison["reparacions_aplicades"]
    col1, col2, col3 = st.columns(3)
    col1.metric(
        "Assignacions inicials",
        comparison["assignacions_originals"],
        border=True,
    )
    col2.metric(
        "Assignacions actives",
        comparison["assignacions_actives"],
        border=True,
    )
    col3.metric(
        "Reparacions del període",
        len(repair_ids),
        border=True,
    )
    if repair_ids:
        st.caption(
            "Reparacions aplicades: "
            + ", ".join(f"R-{repair_id}" for repair_id in repair_ids)
        )
    current = pd.DataFrame(comparison["assignacions"])
    if current.empty:
        st.info("No hi ha assignacions actives en aquest període.")
        return
    visible_columns = [
        "data", "servei", "treballador", "hora_inici", "hora_fi",
        "durada_hores", "zona", "estat",
    ]
    st.dataframe(
        current[visible_columns],
        width="stretch",
        hide_index=True,
        column_config={
            "data": st.column_config.DateColumn("Data", format="DD/MM/YYYY"),
            "durada_hores": st.column_config.NumberColumn(
                "Durada", format="%.2f h"
            ),
        },
    )


def _render_current_plan_differences(comparison: dict) -> None:
    differences = pd.DataFrame(comparison["diferencies"])
    if differences.empty:
        st.success(
            "El pla vigent coincideix amb la proposta inicial en aquest "
            "període."
        )
        return
    st.info(
        f"S'han detectat {len(differences)} diferència/es respecte de la "
        f"fotografia original PI-{comparison['proposta_inicial_id']}."
    )
    differences = differences.rename(
        columns={
            "data": "Data",
            "servei": "Servei",
            "proposta_inicial": "Proposta inicial",
            "pla_vigent": "Pla vigent",
            "estat": "Canvi",
        }
    )
    st.dataframe(
        differences[
            ["Data", "Servei", "Proposta inicial", "Pla vigent", "Canvi"]
        ],
        width="stretch",
        hide_index=True,
        column_config={
            "Data": st.column_config.DateColumn("Data", format="DD/MM/YYYY"),
        },
    )


def _render_review_actions(db_path: str | Path, result: dict) -> None:
    draft_id = result.get("esborrany_id")
    if draft_id is None:
        return
    state = result.get("estat_esborrany", "esborrany")
    state_labels = {
        "esborrany": ("Pendent de revisió", "orange"),
        "validada": ("Validada", "blue"),
        "publicada": ("Publicada · versió inicial", "green"),
        "descartada": ("Descartada", "red"),
    }
    label, color = state_labels.get(state, (state.capitalize(), "gray"))
    st.badge(label, color=color)
    if state == "publicada":
        st.success(
            "Proposta inicial publicada atòmicament al pla operatiu. "
            "Es conserva com a fotografia immutable."
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
                    f"S'ha revertit la publicació de PI-{draft_id}."
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
                    f"La proposta PI-{draft_id} ha quedat validada."
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
                    f"Proposta PI-{draft_id} publicada: "
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
                    f"La proposta PI-{draft_id} ha quedat descartada."
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
        f"Proposta inicial PI-{result['esborrany_id']}"
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
        "fotografia inicial publicada"
        if proposal_state == "publicada"
        else "pla actual sense canvis"
    )
    st.caption(
        f"{proposal_text} · {result['data_inici']} → {result['data_fi']} · "
        f"{state_labels.get(proposal_state, proposal_state)} · {plan_text}"
    )
    if (
        proposal_state == "publicada"
        and db_path is not None
        and result.get("esborrany_id") is not None
    ):
        try:
            current_comparison = compare_initial_draft_with_current_plan(
                db_path,
                int(result["esborrany_id"]),
            )
        except (sqlite3.Error, ValueError) as error:
            st.warning(f"No s'ha pogut reconstruir el pla vigent: {error}")
        else:
            plan_view = st.segmented_control(
                "Vista de la planificació",
                ["Proposta original", "Pla vigent", "Diferències"],
                default="Proposta original",
                key=f"cp_sat_plan_view_{result['esborrany_id']}",
            )
            if plan_view == "Pla vigent":
                _render_current_plan_view(current_comparison)
                return
            if plan_view == "Diferències":
                _render_current_plan_differences(current_comparison)
                return
            st.caption(
                "Aquesta vista conserva el resultat original de PI-"
                f"{result['esborrany_id']}. Les reparacions posteriors només "
                "apareixen a Pla vigent i Diferències."
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


def _scope_plan_summary(
    db_path: str | Path,
    start: date,
    end: date,
    *,
    worker_ids: tuple[str, ...] = (),
    service_ids: tuple[str, ...] = (),
    assignment_ids: tuple[int, ...] = (),
    freeze_until: date | None = None,
) -> dict:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, data, torn, CAST(treballador_id AS TEXT),
                   estat_planificacio
            FROM assig_grup_T
            WHERE data BETWEEN ? AND ?
              AND estat_planificacio IN ('publicada', 'bloquejada')
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        if service_ids:
            placeholders = ",".join("?" for _ in service_ids)
            needs = connection.execute(
                "SELECT COUNT(*) FROM cobertura WHERE data BETWEEN ? AND ? "
                f"AND servei IN ({placeholders})",
                (start.isoformat(), end.isoformat(), *service_ids),
            ).fetchone()[0]
        else:
            needs = connection.execute(
                "SELECT COUNT(*) FROM cobertura WHERE data BETWEEN ? AND ?",
                (start.isoformat(), end.isoformat()),
            ).fetchone()[0]
    selected_workers = set(worker_ids)
    selected_services = set(service_ids)
    selected_assignments = set(assignment_ids)
    in_scope = [
        row
        for row in rows
        if (not selected_workers or row[3] in selected_workers)
        and (not selected_services or row[2] in selected_services)
        and (not selected_assignments or int(row[0]) in selected_assignments)
    ]
    frozen = [
        row
        for row in rows
        if row[4] == "publicada"
        and freeze_until is not None
        and date.fromisoformat(row[1]) <= freeze_until
    ]
    modifiable = [
        row
        for row in in_scope
        if row[4] == "publicada"
        and (
            freeze_until is None
            or date.fromisoformat(row[1]) > freeze_until
        )
    ]
    return {
        "active_assignments": len(rows),
        "locked_assignments": sum(row[4] == "bloquejada" for row in rows),
        "frozen_assignments": len(frozen),
        "in_scope_assignments": len(in_scope),
        "modifiable_assignments": len(modifiable),
        "coverage_needs": int(needs or 0),
        "freeze_until": freeze_until,
    }


def _planning_filter_options(
    db_path: str | Path,
    minimum: date,
    maximum: date,
) -> dict:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        workers = connection.execute(
            """SELECT DISTINCT CAST(t.id AS TEXT) AS id, t.treballador
               FROM treballadors t
               JOIN assig_grup_T a
                 ON CAST(a.treballador_id AS TEXT) = CAST(t.id AS TEXT)
               WHERE a.data BETWEEN ? AND ?
                 AND a.estat_planificacio IN ('publicada', 'bloquejada')
               ORDER BY t.treballador, t.id""",
            (minimum.isoformat(), maximum.isoformat()),
        ).fetchall()
        services = connection.execute(
            """SELECT DISTINCT servei FROM cobertura
               WHERE data BETWEEN ? AND ? ORDER BY servei""",
            (minimum.isoformat(), maximum.isoformat()),
        ).fetchall()
        assignments = connection.execute(
            """SELECT a.id, a.data, a.torn,
                      CAST(a.treballador_id AS TEXT) AS treballador_id,
                      COALESCE(t.treballador, a.treballador_nom,
                               CAST(a.treballador_id AS TEXT)) AS treballador
               FROM assig_grup_T a
               LEFT JOIN treballadors t
                 ON CAST(t.id AS TEXT) = CAST(a.treballador_id AS TEXT)
               WHERE a.data BETWEEN ? AND ?
                 AND a.estat_planificacio IN ('publicada', 'bloquejada')
               ORDER BY a.data, a.torn, a.id""",
            (minimum.isoformat(), maximum.isoformat()),
        ).fetchall()
    return {
        "workers": tuple((str(row["id"]), str(row["treballador"])) for row in workers),
        "services": tuple(str(row["servei"]) for row in services),
        "assignments": tuple(dict(row) for row in assignments),
    }


def _render_selective_planning(
    db_path: str | Path,
    minimum: date,
    maximum: date,
    options: dict,
) -> None:
    worker_names = dict(options["workers"])
    default_end = min(maximum, minimum + timedelta(days=30))
    with st.expander(
        "Gestionar preassignacions",
        icon=":material/lock_clock:",
        expanded=True,
    ):
        st.caption(
            "Crea preassignacions a partir de cobertura abans de calcular el "
            "pla. El planificador haurà d'assignar aquestes necessitats a la "
            "persona indicada."
        )
        active_preassignments = options["preassignments"]
        if active_preassignments:
            st.markdown("**Preassignacions actives**")
            st.dataframe(
                pd.DataFrame(active_preassignments)[
                    ["data", "servei", "treballador", "treballador_id", "motiu"]
                ].rename(
                    columns={
                        "data": "Data",
                        "servei": "Servei",
                        "treballador": "Treballador",
                        "treballador_id": "Identificador",
                        "motiu": "Motiu",
                    }
                ),
                hide_index=True,
                width="stretch",
            )
            preassignments_by_id = {
                int(item["id"]): item for item in active_preassignments
            }

            def format_preassignment(item_id: int) -> str:
                item = preassignments_by_id[item_id]
                formatted_date = date.fromisoformat(item["data"]).strftime(
                    "%d/%m/%Y"
                )
                return (
                    f"{item['servei']} · {formatted_date} · "
                    f"{item['treballador']}"
                )

            selected_preassignment = st.selectbox(
                "Preassignació que vols retirar",
                list(preassignments_by_id),
                index=None,
                placeholder="Selecciona una preassignació",
                format_func=format_preassignment,
                key="planning_selective_revoke",
            )
            if st.button(
                "Retirar preassignació",
                icon=":material/delete:",
                disabled=selected_preassignment is None,
                key="planning_selective_revoke_button",
            ):
                try:
                    revoke_preassignment(db_path, selected_preassignment)
                except (SelectivePlanningError, sqlite3.Error) as error:
                    st.error(str(error), icon=":material/error:")
                else:
                    st.session_state.pop(SELECTIVE_PREVIEW_KEY, None)
                    st.session_state[NOTICE_KEY] = "Preassignació retirada."
                    st.rerun()
        start_column, end_column = st.columns(2)
        selective_start = start_column.date_input(
            "Data d'inici de la reserva",
            value=minimum,
            min_value=minimum,
            max_value=maximum,
            key="planning_selective_start",
        )
        selective_end = end_column.date_input(
            "Data final de la reserva",
            value=default_end,
            min_value=minimum,
            max_value=maximum,
            key="planning_selective_end",
        )
        active_need_ids = {
            item["necessitat_id"] for item in options["preassignments"]
        }
        selectable_needs = {
            item["need_id"]: item
            for item in options["needs"]
            if (
                selective_start.isoformat()
                <= item["date"]
                <= selective_end.isoformat()
                and item["need_id"] not in active_need_ids
            )
        }

        def format_need(need_id: str) -> str:
            item = selectable_needs[need_id]
            formatted_date = date.fromisoformat(item["date"]).strftime("%d/%m/%Y")
            return f"{item['service_id']} · {formatted_date}"

        with st.form("planning_selective_assignment"):
            worker_id = st.selectbox(
                "Treballador que quedarà fixat",
                list(worker_names),
                index=None,
                placeholder="Selecciona un treballador",
                format_func=lambda item: f"{worker_names[item]} · {item}",
                key="planning_selective_worker",
            )
            need_ids = st.multiselect(
                "Serveis i dies que vols reservar",
                list(selectable_needs),
                format_func=format_need,
                placeholder="Selecciona un o diversos serveis amb el seu dia",
                disabled=not selectable_needs,
                key="planning_selective_needs",
            )
            reason = st.text_input(
                "Motiu",
                value="Assignació fixada abans de planificar",
                key="planning_selective_reason",
            )
            preview_requested = st.form_submit_button(
                "Previsualitzar assignacions",
                icon=":material/preview:",
                width="stretch",
            )
        if preview_requested:
            try:
                st.session_state[SELECTIVE_PREVIEW_KEY] = (
                    preview_preassignments(
                        db_path,
                        start_date=selective_start,
                        end_date=selective_end,
                        worker_id=worker_id,
                        need_ids=need_ids,
                        reason=reason,
                    )
                )
            except (SelectivePlanningError, sqlite3.Error, ValueError) as error:
                st.session_state.pop(SELECTIVE_PREVIEW_KEY, None)
                st.error(str(error), icon=":material/error:")

        preview = st.session_state.get(SELECTIVE_PREVIEW_KEY)
        if preview and (
            preview["start_date"] != selective_start.isoformat()
            or preview["end_date"] != selective_end.isoformat()
            or preview["worker_id"] != str(worker_id or "")
            or set(preview["need_ids"]) != set(need_ids)
            or preview["reason"]
            != (reason.strip() or "Assignació fixada abans de planificar")
        ):
            st.session_state.pop(SELECTIVE_PREVIEW_KEY, None)
            preview = None
        if not preview:
            return
        st.markdown("**Preassignacions que es desaran**")
        assignments = pd.DataFrame(preview["assignments"])
        st.dataframe(
            assignments[
                [
                    "data",
                    "servei",
                    "treballador",
                    "treballador_id",
                ]
            ].rename(
                columns={
                    "data": "Data",
                    "servei": "Servei",
                    "treballador": "Treballador",
                    "treballador_id": "Identificador",
                }
            ),
            hide_index=True,
            width="stretch",
        )
        st.success(
            f"Es poden desar {len(preview['assignments'])} preassignació/ns.",
            icon=":material/check_circle:",
        )
        confirmed = st.checkbox(
            "Confirmo que vull fixar aquestes opcions per al pròxim càlcul",
            key="planning_selective_confirm",
        )
        if st.button(
            "Desar preassignacions",
            type="primary",
            icon=":material/lock:",
            width="stretch",
            disabled=not confirmed,
            key="planning_selective_save",
        ):
            try:
                result = save_preassignments(
                    db_path,
                    start_date=date.fromisoformat(preview["start_date"]),
                    end_date=date.fromisoformat(preview["end_date"]),
                    worker_id=preview["worker_id"],
                    need_ids=preview["need_ids"],
                    reason=preview["reason"],
                )
            except Exception as error:
                st.error(
                    f"No s'han pogut desar les preassignacions: {error}",
                    icon=":material/error:",
                )
            else:
                st.session_state.pop(SELECTIVE_PREVIEW_KEY, None)
                st.session_state[NOTICE_KEY] = (
                    f"{result['saved_count']} preassignació/ns desada/es. "
                    "S'aplicaran obligatòriament al pròxim càlcul."
                )
                st.rerun()


def _execution_presentation(
    execution: StoredPlanningExecution,
    *,
    stale_message: str | None = None,
) -> dict:
    state_labels = {
        "esborrany": "Pendent de revisió",
        "validada": "Validada",
        "publicada": "Publicació completada",
        "descartada": "Descartada",
        "revertida": "Revertida",
    }
    state = "obsoleta" if stale_message else execution.state
    labels = {**state_labels, "obsoleta": "Proposta obsoleta"}
    rows = []
    impact: dict[str, dict] = {}

    def add_impact(worker_id: str | None, hours: float, assignments: int) -> None:
        if worker_id is None:
            return
        item = impact.setdefault(
            worker_id,
            {"Treballador": worker_id, "Canvi assignacions": 0, "Canvi hores": 0.0},
        )
        item["Canvi assignacions"] += assignments
        item["Canvi hores"] = round(item["Canvi hores"] + hours, 2)

    kind_labels = {
        "alta": "Afegida",
        "baixa": "Eliminada",
        "reassignacio": "Reassignada",
    }
    for change in execution.changes:
        before_worker = change.previous.worker_id if change.previous else None
        after_worker = change.proposed.worker_id if change.proposed else None
        duration = (
            change.proposed.duration_minutes / 60
            if change.proposed
            else change.previous.duration_minutes / 60
            if change.previous
            else 0.0
        )
        add_impact(before_worker, -duration, -1)
        add_impact(after_worker, duration, 1)
        rows.append(
            {
                "Data": change.date.isoformat(),
                "Servei": change.service_id,
                "Canvi": kind_labels[change.kind.value],
                "Pla vigent": before_worker or "Descobert",
                "Proposta": after_worker or "Descobert",
                "Motiu": change.reason or "Canvi proposat pel planificador",
            }
        )
    return {
        "state": state,
        "state_label": labels[state],
        "stale_message": stale_message,
        "unchanged": execution.unchanged_assignments,
        "changes": execution.persistent_changes,
        "reassignments": sum(
            change.kind.value == "reassignacio" for change in execution.changes
        ),
        "additions": sum(
            change.kind.value == "alta" for change in execution.changes
        ),
        "removals": sum(
            change.kind.value == "baixa" for change in execution.changes
        ),
        "uncovered": execution.uncovered_needs,
        "comparison": rows,
        "worker_impact": sorted(impact.values(), key=lambda item: item["Treballador"]),
        "uncovered_details": execution.metrics.get("uncovered", []),
        "equity_assessment": execution.metrics.get(
            "equity_assessment",
            {
                "status": "no_avaluada",
                "publishable": False,
                "reasons": ["avaluacio_equitat_absent"],
            },
        ),
        "equity_diagnostics": execution.metrics.get(
            "equity_diagnostics", []
        ),
        "equity_override": getattr(execution, "equity_override", None),
    }


def _render_scope_plan_summary(summary: dict) -> None:
    st.markdown("#### Pla actiu dins del període")
    with st.container(horizontal=True):
        st.metric(
            "Assignacions actives al període",
            summary["active_assignments"],
            border=True,
        )
        st.metric(
            "Protegides per bloqueig",
            summary["locked_assignments"],
            border=True,
        )
        st.metric(
            "Protegides per data",
            summary["frozen_assignments"],
            border=True,
        )
        st.metric(
            "Coincideixen amb els filtres",
            summary["in_scope_assignments"],
            border=True,
        )
        st.metric(
            "Realment modificables",
            summary["modifiable_assignments"],
            border=True,
        )
        st.metric(
            "Necessitats de cobertura",
            summary["coverage_needs"],
            border=True,
        )
    if summary["freeze_until"] is not None:
        st.caption(
            "Assignacions protegides fins al "
            f"**{_data_llegible(summary['freeze_until'])}**, inclòs."
        )


def _render_incremental_actions(
    db_path: str | Path,
    execution: StoredPlanningExecution,
) -> None:
    rollout = load_planning_rollout_config()
    assessment = getattr(execution, "metrics", {}).get(
        "equity_assessment", {}
    )
    solver_status = getattr(execution, "solver_status", "OPTIMAL")
    publishable = solver_status in PUBLISHABLE_SOLVER_STATUSES
    optimality_certified = assessment.get("technical_ready", False)
    if execution.state == "esborrany":
        if publishable and not optimality_certified:
            st.warning(
                "La proposta és factible però no acredita l'optimalitat. "
                "Es pot validar i, després, publicar sota aquesta condició."
            )
        confirmed = st.checkbox(
            "Confirmo que he revisat els canvis i els descoberts",
            key=f"planning_validate_confirm_{execution.id}",
        )
        if st.button(
            "Validar proposta",
            type="primary",
            icon=":material/check_circle:",
            disabled=not confirmed or not publishable,
            key=f"planning_validate_{execution.id}",
        ):
            try:
                validate_planning_execution(db_path, execution.id)
                st.session_state[NOTICE_KEY] = "La proposta ha quedat validada."
                st.session_state.pop(STALE_KEY, None)
                st.rerun()
            except PlanningExecutionStaleError as error:
                st.session_state[STALE_KEY] = str(error)
                st.error(str(error))
            except (sqlite3.Error, ValueError) as error:
                st.error(str(error))

    if execution.state == "validada":
        if publishable and not optimality_certified:
            st.warning(
                "Resultat FEASIBLE: compleix les restriccions dures, però "
                "el solver no ha demostrat que sigui la millor solució."
            )
        if not rollout.publication_enabled:
            st.info(
                "Mode ombra actiu: pots revisar i validar la proposta, però "
                "la publicació està desactivada."
            )
        confirmed = st.checkbox(
            (
                "Confirmo que vull publicar aquest resultat FEASIBLE i "
                "accepto que no s'ha demostrat l'optimalitat"
                if solver_status == "FEASIBLE"
                else "Confirmo que vull publicar exclusivament aquests canvis"
            ),
            key=f"planning_publish_confirm_{execution.id}",
        )
        if st.button(
            "Publicar canvis",
            type="primary",
            icon=":material/publish:",
            disabled=(
                not confirmed
                or not rollout.publication_enabled
                or not publishable
            ),
            key=f"planning_publish_{execution.id}",
        ):
            try:
                result = apply_planning_changeset(db_path, execution.id)
                st.session_state[NOTICE_KEY] = (
                    f"Publicació completada: "
                    f"{len(result['new_assignment_ids'])} altes."
                )
                st.rerun()
            except (sqlite3.Error, ValueError) as error:
                st.error(str(error))

    if execution.state in {"esborrany", "validada"}:
        confirmed = st.checkbox(
            "Confirmo que vull descartar aquesta proposta",
            key=f"planning_discard_confirm_{execution.id}",
        )
        if st.button(
            "Descartar proposta",
            icon=":material/delete:",
            disabled=not confirmed,
            key=f"planning_discard_{execution.id}",
        ):
            try:
                discard_planning_execution(db_path, execution.id)
                st.session_state[NOTICE_KEY] = "La proposta ha quedat descartada."
                st.rerun()
            except (sqlite3.Error, ValueError) as error:
                st.error(str(error))

    if execution.state == "publicada":
        audit = load_planning_publication_audit(db_path, execution.id)
        st.caption(f"Còpia de seguretat: {Path(audit.backup_path).name}")
        confirmed = st.checkbox(
            "Confirmo que vull revertir només els canvis d'aquesta publicació",
            key=f"planning_rollback_confirm_{execution.id}",
        )
        if st.button(
            "Revertir publicació",
            icon=":material/undo:",
            disabled=not confirmed,
            key=f"planning_rollback_{execution.id}",
        ):
            try:
                rollback_planning_changeset(db_path, execution.id)
                st.session_state[NOTICE_KEY] = "La publicació s'ha revertit."
                st.rerun()
            except (sqlite3.Error, ValueError) as error:
                st.error(str(error))


def _render_incremental_execution(
    db_path: str | Path,
    execution: StoredPlanningExecution,
) -> None:
    stale_message = st.session_state.get(STALE_KEY)
    view = _execution_presentation(execution, stale_message=stale_message)
    selected_engine = normalize_solver_engine(
        execution.configuration.get("solver_engine", SolverEngine.CURRENT)
    )
    st.subheader(f"Revisió P-{execution.id}")
    st.caption(
        f"{_data_llegible(execution.request.scope.start_date)} – "
        f"{_data_llegible(execution.request.scope.end_date)} · "
        f"{view['state_label']} · "
        f"Motor: {solver_engine_label(selected_engine)}"
    )
    st.badge(
        f"Solver {execution.solver_status}",
        color="green" if execution.solver_status == "OPTIMAL" else "orange",
    )
    scope_details = []
    if execution.request.scope.service_ids:
        scope_details.append(
            f"{len(execution.request.scope.service_ids)} servei/s origen"
        )
    if execution.request.scope.worker_ids:
        scope_details.append(
            f"{len(execution.request.scope.worker_ids)} treballador/s origen"
        )
    if execution.request.scope.assignment_ids:
        scope_details.append(
            f"{len(execution.request.scope.assignment_ids)} assignació/ns origen"
        )
    if not execution.request.scope.worker_ids:
        scope_details.append("tots els treballadors elegibles poden rebre")
    elif execution.request.protection.allow_unselected_workers_as_recipients:
        scope_details.append("receptors externs permesos")
    else:
        scope_details.append("receptors limitats a la selecció")
    if execution.request.protection.freeze_until is not None:
        scope_details.append(
            "congelat fins al "
            f"{_data_llegible(execution.request.protection.freeze_until)}"
        )
    st.caption("Abast: " + " · ".join(scope_details))
    shadow = planning_shadow_report(execution)
    st.caption(
        "Comparació ombra: "
        f"{shadow['coverage_percent']:.2f}% cobertura · "
        f"{shadow['unchanged_assignments']} conservades · "
        f"{shadow['persistent_changes']} canvis · "
        f"{float(shadow['wall_time_seconds'] or 0):.2f} s"
    )
    equity_assessment = view["equity_assessment"]
    optimality_certified = equity_assessment.get("technical_ready", False)
    if optimality_certified and not equity_assessment.get("review_worker_ids", ()):
        if selected_engine is SolverEngine.PRIORITY:
            st.success(
                "Totes les fases del motor per prioritats han acabat en estat "
                "òptim."
            )
        else:
            st.success(
                "Equitat contractual avaluada. La referència del 75% s'ha "
                "usat com a criteri secundari després de maximitzar la "
                "cobertura."
            )
    elif optimality_certified:
        workers = ", ".join(equity_assessment.get("review_worker_ids", ()))
        affected = f" Treballadors destacats: {workers}." if workers else ""
        st.warning(
            "El diagnòstic posterior mostra diferències que convé revisar. "
            "Són informatives i no bloquegen la validació ni la publicació."
            + affected
        )
    else:
        reasons = ", ".join(
            equity_assessment.get("reasons", ["avaluació no disponible"])
        )
        st.warning(
            "Resultat factible i publicable, però sense optimalitat "
            f"demostrada ({reasons})."
        )
    if view["stale_message"]:
        st.error(view["stale_message"], icon=":material/update_disabled:")
    elif execution.state == "publicada":
        st.success("Publicació completada i disponible per a rollback controlat.")
    elif execution.state == "revertida":
        st.info("Aquesta publicació ja ha estat revertida.")

    with st.container(horizontal=True):
        st.metric("Conservades", view["unchanged"], border=True)
        st.metric("Reassignades", view["reassignments"], border=True)
        st.metric("Afegides", view["additions"], border=True)
        st.metric("Eliminades", view["removals"], border=True)
        st.metric("Descobertes", view["uncovered"], border=True)

    changes_tab, uncovered_tab, impact_tab = st.tabs(
        ["Canvis", "Descoberts", "Impacte i equitat"]
    )
    with changes_tab:
        if view["comparison"]:
            st.dataframe(
                pd.DataFrame(view["comparison"]),
                hide_index=True,
                width="stretch",
                column_config={
                    "Data": st.column_config.DateColumn(
                        "Data",
                        format="DD/MM/YYYY",
                    )
                },
            )
        else:
            st.success("La proposta conserva tot el pla vigent: zero canvis.")

    with uncovered_tab:
        if view["uncovered_details"]:
            st.dataframe(
                pd.DataFrame(view["uncovered_details"])[["need_id", "reason"]]
                .rename(columns={"need_id": "Necessitat", "reason": "Motiu"}),
                hide_index=True,
                width="stretch",
            )
        else:
            st.success("No hi ha necessitats descobertes.")

    with impact_tab:
        if view["worker_impact"]:
            st.markdown("**Impacte per treballador**")
            st.dataframe(
                pd.DataFrame(view["worker_impact"]),
                hide_index=True,
                width="stretch",
                column_config={
                    "Canvi hores": st.column_config.NumberColumn(
                        "Canvi hores",
                        format="%+.2f h",
                    )
                },
            )
        else:
            st.info("La proposta no modifica la càrrega de cap treballador.")

        diagnostics = view["equity_diagnostics"]
        if diagnostics:
            comparable = [item for item in diagnostics if item.get("comparable")]
            with st.expander(
                "Diagnòstic justificable per treballador",
                icon=":material/balance:",
            ):
                st.caption(
                    "L'equitat compara exclusivament el grup T. L'objectiu base "
                    "és el 75% de 1.605 hores i només s'ajusta proporcionalment "
                    "per les absències pròpies de cada treballador."
                )
                st.dataframe(
                    [
                        {
                            "Treballador": item.get("worker_id"),
                            "% objectiu ajustat": round(
                                float(item.get("completion_rate_permille", 0)) / 10,
                                1,
                            ),
                            "Objectiu base T (h)": round(
                                float(item.get("base_target_minutes", 0)) / 60,
                                1,
                            ),
                            "Sostre (h)": round(
                                float(item.get("maximum_minutes", 0)) / 60,
                                1,
                            ),
                            "Diferència vs. grup (pp)": round(
                                float(item.get("peer_gap_permille", 0)) / 10,
                                1,
                            ),
                            "Hores acumulades": round(
                                float(item.get("annual_minutes", 0)) / 60, 1
                            ),
                            "Objectiu ajustat (h)": round(
                                float(item.get("adjusted_target_minutes", 0)) / 60,
                                1,
                            ),
                            "Dies de baixa": item.get("absence_days", 0),
                            "Capacitat compatible (h)": round(
                                float(
                                    item.get("compatible_opportunity_minutes", 0)
                                )
                                / 60,
                                1,
                            ),
                            "Revisió": item.get("review_status"),
                            "Justificació": ", ".join(
                                item.get("justification_codes", [])
                            ),
                        }
                        for item in diagnostics
                    ],
                    hide_index=True,
                    width="stretch",
                )
                st.caption(
                    f"{len(comparable)} de {len(diagnostics)} treballadors "
                    "formen part de la comparació central."
                )

    st.markdown("#### Decisió")
    _render_incremental_actions(db_path, execution)


def _planning_execution_rows(executions: list[StoredPlanningExecution]) -> list[dict]:
    return [
        {
            "Proposta": f"P-{item.id}",
            "Estat": _execution_presentation(item)["state_label"],
            "Motor": solver_engine_label(
                item.configuration.get("solver_engine", SolverEngine.CURRENT)
            ),
            "Inici": item.request.scope.start_date,
            "Final": item.request.scope.end_date,
            "Conservades": item.unchanged_assignments,
            "Canvis": item.persistent_changes,
            "Descobertes": item.uncovered_needs,
        }
        for item in executions
    ]


def _render_saved_proposals(
    db_path: str | Path,
    executions: list[StoredPlanningExecution],
) -> None:
    execution_id = st.session_state.get(EXECUTION_ID_KEY)
    if execution_id is not None:
        if st.button(
            "Tornar a la llista",
            icon=":material/arrow_back:",
            type="tertiary",
            key="planning_close_execution",
        ):
            st.session_state.pop(EXECUTION_ID_KEY, None)
            st.session_state.pop(STALE_KEY, None)
            st.rerun()
        try:
            execution = load_planning_execution(db_path, int(execution_id))
        except (sqlite3.Error, ValueError) as error:
            st.error(str(error))
        else:
            _render_incremental_execution(db_path, execution)
        return

    st.subheader("Revisar propostes")
    st.caption(
        "Obre una proposta pendent per validar-la o consulta l'històric de "
        "publicacions i descartaments."
    )
    if not executions:
        st.info("Encara no hi ha cap proposta incremental desada.")
        return

    pending = [
        item
        for item in executions
        if item.state not in {"descartada", "publicada", "revertida"}
    ]
    history = [item for item in executions if item not in pending]
    if pending:
        st.markdown("**Pendents**")
        st.dataframe(
            pd.DataFrame(_planning_execution_rows(pending)),
            hide_index=True,
            width="stretch",
        )
    else:
        st.success("No hi ha propostes pendents de decisió.")

    if history:
        with st.expander(
            f"Històric ({len(history)})",
            icon=":material/history:",
        ):
            st.dataframe(
                pd.DataFrame(_planning_execution_rows(history)),
                hide_index=True,
                width="stretch",
            )

    execution_by_id = {item.id: item for item in executions}
    ordered_ids = [item.id for item in pending] + [item.id for item in history]
    selected_id = st.selectbox(
        "Proposta que vols obrir",
        ordered_ids,
        index=None,
        placeholder="Selecciona una proposta",
        format_func=lambda item_id: (
            f"P-{item_id} · "
            f"{_execution_presentation(execution_by_id[item_id])['state_label']}"
        ),
        key="planning_saved_execution_id",
    )
    if st.button(
        "Obrir proposta",
        icon=":material/visibility:",
        type="primary",
        disabled=selected_id is None,
        key="planning_open_execution",
    ):
        st.session_state[EXECUTION_ID_KEY] = int(selected_id)
        st.session_state.pop(STALE_KEY, None)
        st.rerun()


def _scope_review_payload(
    *,
    start: date,
    end: date,
    worker_ids: list[str],
    service_ids: list[str],
    assignment_ids: list[int],
    freeze_until: date | None,
    allow_unselected_recipients: bool,
    time_limit: float,
    num_workers: int,
    force_seeds: bool,
    solver_engine: SolverEngine | str,
) -> dict:
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "worker_ids": list(worker_ids),
        "service_ids": list(service_ids),
        "assignment_ids": [int(item) for item in assignment_ids],
        "freeze_until": freeze_until.isoformat() if freeze_until else None,
        "allow_unselected_recipients": allow_unselected_recipients,
        "time_limit": float(time_limit),
        "num_workers": int(num_workers),
        "force_seeds": bool(force_seeds),
        "solver_engine": normalize_solver_engine(solver_engine).value,
    }


def render_pestanya_planificacio_cp_sat(db_path: str | Path) -> None:
    st.header("Planificació")
    st.caption(
        "Revisa el pla vigent, genera una proposta incremental i publica "
        "exclusivament els canvis confirmats."
    )
    try:
        rollout = load_planning_rollout_config()
    except ValueError as error:
        st.error(str(error))
        return
    if rollout.is_shadow:
        st.badge("Mode ombra", icon=":material/visibility:", color="blue")
        st.caption(
            "La simulació i la validació estan actives; la publicació "
            "incremental està desactivada per configuració."
        )
    elif rollout.publication_enabled:
        st.badge(
            "Publicació incremental habilitada",
            icon=":material/publish:",
            color="green",
        )

    view_override = st.session_state.pop(WORKSPACE_VIEW_OVERRIDE_KEY, None)
    if view_override is not None:
        st.session_state[WORKSPACE_VIEW_KEY] = view_override
    workspace_view = st.segmented_control(
        "Què vols fer?",
        (VIEW_NEW_PROPOSAL, VIEW_SAVED_PROPOSALS, VIEW_PREASSIGNMENTS),
        default=VIEW_NEW_PROPOSAL,
        required=True,
        width="stretch",
        key=WORKSPACE_VIEW_KEY,
    )

    notice = st.session_state.pop(NOTICE_KEY, None)
    if notice:
        st.success(notice)
    try:
        migrate_planning_schema(db_path)
        executions = list_planning_executions(db_path)
    except (sqlite3.Error, ValueError) as error:
        st.error(f"No s'ha pogut preparar la planificació: {error}")
        return

    if workspace_view == VIEW_SAVED_PROPOSALS:
        _render_saved_proposals(db_path, executions)
        return

    try:
        minimum, maximum = _coverage_limits(db_path)
    except (sqlite3.Error, ValueError) as error:
        st.error(f"No s'ha pogut preparar la planificació: {error}")
        return

    if workspace_view == VIEW_PREASSIGNMENTS:
        try:
            selective_options = load_selective_planning_options(db_path)
        except (sqlite3.Error, ValueError) as error:
            st.error(f"No s'han pogut carregar les preassignacions: {error}")
            return
        st.subheader("Preassignacions")
        st.caption(
            "Fixa cobertures concretes perquè el pròxim càlcul les respecti. "
            "Aquesta operació no publica ni modifica el pla vigent."
        )
        _render_selective_planning(
            db_path,
            minimum,
            maximum,
            selective_options,
        )
        return

    try:
        filter_options = _planning_filter_options(db_path, minimum, maximum)
    except (sqlite3.Error, ValueError) as error:
        st.error(f"No s'han pogut carregar els filtres: {error}")
        return

    st.subheader("Generar una proposta")
    default_end = min(maximum, minimum + timedelta(days=30))
    st.caption(
        "Rang disponible: "
        f"**{_data_llegible(minimum)} – {_data_llegible(maximum)}**."
    )
    worker_names = dict(filter_options["workers"])
    assignment_by_id = {
        int(item["id"]): item for item in filter_options["assignments"]
    }
    with st.form("planning_incremental_proposal"):
        start_column, end_column = st.columns(2)
        with start_column:
            start = st.date_input(
                "Data d'inici",
                value=minimum,
                min_value=minimum,
                max_value=maximum,
                key="planning_start",
            )
        with end_column:
            end = st.date_input(
                "Data final",
                value=default_end,
                min_value=minimum,
                max_value=maximum,
                key="planning_end",
            )
        solver_engine = st.segmented_control(
            "Motor de planificació",
            (SolverEngine.CURRENT.value, SolverEngine.PRIORITY.value),
            default=SolverEngine.CURRENT.value,
            required=True,
            width="stretch",
            format_func=solver_engine_label,
            key="planning_solver_engine",
            help=(
                "El motor vigent conserva el comportament actual. El motor "
                "nou aplica les prioritats acumulatives en paral·lel."
            ),
        )
        with st.expander(
            "Opcions de l'abast",
            icon=":material/filter_alt:",
        ):
            st.markdown("**Assignacions que es poden revisar**")
            st.caption(
                "Si deixes un selector buit, no limitarà les assignacions "
                "per aquell criteri."
            )
            service_ids = st.multiselect(
                "Modificar assignacions per serveis",
                filter_options["services"],
                placeholder="Tots els serveis",
                key="planning_service_ids",
            )
            worker_ids = st.multiselect(
                "Modificar assignacions per persones",
                list(worker_names),
                placeholder="Tots els treballadors",
                format_func=lambda worker_id: worker_names[worker_id],
                key="planning_worker_ids",
                help=(
                    "La selecció limita quines assignacions es poden canviar; "
                    "no assigna automàticament aquests treballadors."
                ),
            )
            assignment_ids = st.multiselect(
                "Modificar per assignacions",
                list(assignment_by_id),
                placeholder="Totes les assignacions que compleixen els filtres",
                format_func=lambda assignment_id: (
                    f"{_data_llegible(assignment_by_id[assignment_id]['data'])} · "
                    f"{assignment_by_id[assignment_id]['torn']} · "
                    f"{assignment_by_id[assignment_id]['treballador']}"
                ),
                key="planning_assignment_ids",
            )
            st.markdown("**Assignacions protegides**")
            freeze_until = st.date_input(
                "Protegir les assignacions fins al dia",
                value=None,
                min_value=minimum,
                max_value=maximum,
                key="planning_freeze_until",
                help=(
                    "Les assignacions fins a aquesta data, inclosa, no es "
                    "podran modificar."
                ),
            )
            st.markdown("**Persones que poden rebre cobertures**")
            recipient_scope = st.segmented_control(
                "Persones que poden rebre noves cobertures",
                (
                    "Seguir el filtre de persones",
                    "Qualsevol persona elegible",
                ),
                default="Seguir el filtre de persones",
                required=True,
                width="stretch",
                key="planning_recipient_scope",
                help=(
                    "Si no has limitat les assignacions per persones, totes "
                    "les persones elegibles podran rebre cobertures."
                ),
            )
            allow_unselected_recipients = (
                recipient_scope == "Qualsevol persona elegible"
            )
        with st.expander("Configuració avançada", icon=":material/tune:"):
            time_column, workers_column = st.columns(2)
            with time_column:
                time_limit = st.number_input(
                    "Temps total de replanificació (s)",
                    5,
                    300,
                    120,
                    step=5,
                    help=(
                        "Pressupost compartit per cobertura, estabilitat, "
                        "serveis fora de preferència i equitat social. "
                        "El motor vigent conserva els seus límits interns; "
                        "el motor nou pot necessitar més temps en períodes "
                        "llargs."
                    ),
                )
            with workers_column:
                num_workers = st.number_input(
                    "Processos de càlcul",
                    1,
                    16,
                    8,
                    step=1,
                )
            force_seeds = st.checkbox("Cercar alternatives addicionals")
        review_scope = st.form_submit_button(
            "Revisar abast",
            type="primary",
            icon=":material/fact_check:",
            width="stretch",
        )

    if review_scope:
        validation_error = None
        if start > end:
            validation_error = (
                "La data d'inici no pot ser posterior a la data final."
            )
        elif freeze_until is not None and not (start <= freeze_until <= end):
            validation_error = (
                "La data de congelació ha de quedar dins del rang seleccionat."
            )
        else:
            assignments_outside_range = [
                assignment_id
                for assignment_id in assignment_ids
                if not (
                    start
                    <= date.fromisoformat(assignment_by_id[assignment_id]["data"])
                    <= end
                )
            ]
            if assignments_outside_range:
                validation_error = (
                    "Totes les assignacions seleccionades han de quedar dins "
                    "del rang de dates."
                )
        if validation_error:
            st.session_state.pop(SCOPE_REVIEW_KEY, None)
            st.error(validation_error)
        else:
            st.session_state[SCOPE_REVIEW_KEY] = _scope_review_payload(
                start=start,
                end=end,
                worker_ids=worker_ids,
                service_ids=service_ids,
                assignment_ids=assignment_ids,
                freeze_until=freeze_until,
                allow_unselected_recipients=allow_unselected_recipients,
                time_limit=time_limit,
                num_workers=num_workers,
                force_seeds=force_seeds,
                solver_engine=solver_engine,
            )
            st.rerun()

    reviewed_scope = st.session_state.get(SCOPE_REVIEW_KEY)
    if not reviewed_scope:
        st.info(
            "Defineix el període i, si cal, ajusta les opcions. Després "
            "revisa l'abast abans d'executar el càlcul."
        )
        return

    reviewed_start = date.fromisoformat(reviewed_scope["start"])
    reviewed_end = date.fromisoformat(reviewed_scope["end"])
    reviewed_freeze = (
        date.fromisoformat(reviewed_scope["freeze_until"])
        if reviewed_scope["freeze_until"]
        else None
    )
    scope_summary = _scope_plan_summary(
        db_path,
        reviewed_start,
        reviewed_end,
        worker_ids=tuple(reviewed_scope["worker_ids"]),
        service_ids=tuple(reviewed_scope["service_ids"]),
        assignment_ids=tuple(reviewed_scope["assignment_ids"]),
        freeze_until=reviewed_freeze,
    )
    with st.container(border=True):
        st.markdown("#### Abast preparat")
        st.caption(
            f"{_data_llegible(reviewed_start)} – "
            f"{_data_llegible(reviewed_end)}. El càlcul utilitzarà exactament "
            "aquesta configuració revisada. "
            f"Motor: **{solver_engine_label(reviewed_scope.get('solver_engine', SolverEngine.CURRENT))}**."
        )
        with st.container(horizontal=True):
            st.metric(
                "Necessitats",
                scope_summary["coverage_needs"],
                border=True,
            )
            st.metric(
                "Modificables",
                scope_summary["modifiable_assignments"],
                border=True,
            )
            st.metric(
                "Protegides",
                max(
                    0,
                    scope_summary["active_assignments"]
                    - scope_summary["modifiable_assignments"],
                ),
                border=True,
            )
        with st.expander("Veure el detall de l'abast"):
            _render_scope_plan_summary(scope_summary)
        generate = st.button(
            "Generar proposta",
            type="primary",
            icon=":material/play_arrow:",
            width="stretch",
            key="planning_generate_reviewed_scope",
        )

    if generate:
        start = reviewed_start
        end = reviewed_end
        worker_ids = reviewed_scope["worker_ids"]
        service_ids = reviewed_scope["service_ids"]
        assignment_ids = reviewed_scope["assignment_ids"]
        freeze_until = reviewed_freeze
        allow_unselected_recipients = reviewed_scope[
            "allow_unselected_recipients"
        ]
        time_limit = reviewed_scope["time_limit"]
        num_workers = reviewed_scope["num_workers"]
        force_seeds = reviewed_scope["force_seeds"]
        solver_engine = reviewed_scope.get(
            "solver_engine", SolverEngine.CURRENT.value
        )
        try:
            from cp_sat_pilot import SolverConfig
            from planificador_cp_sat.domain import (
                PlanningExecutionRequest,
                PlanningScope,
                ProtectionPolicy,
            )
            from planificador_cp_sat.services.preparacio_planificacio import (
                prepare_planning_problem,
            )
            from planificador_cp_sat.services.proposta_planificacio import (
                generate_planning_proposal,
            )

            with st.spinner("Calculant cobertura i canvis mínims…"):
                prepared = prepare_planning_problem(
                    db_path,
                    PlanningExecutionRequest(
                        scope=PlanningScope(
                            start,
                            end,
                            worker_ids=worker_ids,
                            service_ids=service_ids,
                            assignment_ids=assignment_ids,
                        ),
                        protection=ProtectionPolicy(
                            freeze_until=freeze_until,
                            allow_unselected_workers_as_recipients=(
                                allow_unselected_recipients
                            ),
                        ),
                    ),
                )
                proposal = generate_planning_proposal(
                    prepared,
                    config=SolverConfig(
                        max_time_seconds=float(time_limit),
                        num_workers=int(num_workers),
                        random_seed=0,
                    ),
                    seeds=(0, 1, 2),
                    force_all_seeds=force_seeds,
                    solver_engine=solver_engine,
                )
                execution_id = save_planning_proposal(db_path, proposal)
            st.session_state[EXECUTION_ID_KEY] = execution_id
            st.session_state[WORKSPACE_VIEW_OVERRIDE_KEY] = VIEW_SAVED_PROPOSALS
            st.session_state.pop(SCOPE_REVIEW_KEY, None)
            st.session_state.pop(STALE_KEY, None)
            st.rerun()
        except (sqlite3.Error, ValueError) as error:
            st.error(str(error))
