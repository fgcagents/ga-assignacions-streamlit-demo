"""Interfície de la primera versió controlada de replanificació per incidències."""

from datetime import date
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from servei_descansos import llista_treballadors
from servei_incidencies import (
    TIPUS_INCIDENCIA, aprovar_proposta, generar_proposta, inicialitza_planificacio,
    llista_incidencies, llista_propostes, obtenir_proposta, registrar_incidencia,
)
from ui_components import claus_selector_treballador, selector_treballador


def _etiqueta(t: dict) -> str:
    return f"{t['treballador']} · {t['plaza']} (ID {t['id']})"


def _reinicia_registre_incidencia() -> None:
    claus = [
        *claus_selector_treballador("incidencia_treballador"),
        *claus_selector_treballador("incidencia_substitut"),
        "incidencia_tipus", "incidencia_comunicacio", "incidencia_inici",
        "incidencia_fi", "incidencia_motiu", "incidencia_confirma",
    ]
    for clau in claus:
        st.session_state.pop(clau, None)


ETIQUETES_TIPUS = {
    "baixa": "Baixa", "vacances": "Vacances", "substitucio": "Substitució",
    "alta_anticipada": "Alta anticipada", "prorroga_baixa": "Pròrroga de baixa",
}


def _clau_canvi(canvi: dict) -> str:
    assignacio_id = canvi.get("assignacio_id")
    if assignacio_id is not None:
        return f"assignacio:{assignacio_id}"
    return f"necessitat:{canvi.get('necessitat_id')}"


def _resum_operatiu(detall: dict) -> dict[str, int | float | None]:
    canvis = detall.get("canvis", [])
    total = detall.get("necessitats_totals")
    cobertes = detall.get("necessitats_cobertes")
    percentatge = (
        100 * int(cobertes) / int(total)
        if total not in (None, 0) and cobertes is not None
        else None
    )
    return {
        "cobertes": cobertes,
        "total": total,
        "percentatge": percentatge,
        "afectades": sum(
            canvi["tipus"] == "assignacio_a_reemplaçar" for canvi in canvis
        ),
        "proposades": sum(
            canvi["tipus"] == "assignacio_proposada" for canvi in canvis
        ),
        "descobertes": sum(
            canvi["tipus"] == "servei_sense_cobertura" for canvi in canvis
        ),
    }


def _comparacio_operativa(canvis: list[dict]) -> pd.DataFrame:
    afectades = {
        _clau_canvi(canvi): canvi
        for canvi in canvis
        if canvi["tipus"] == "assignacio_a_reemplaçar"
    }
    proposades = {
        _clau_canvi(canvi): canvi
        for canvi in canvis
        if canvi["tipus"] == "assignacio_proposada"
    }
    descobertes = {
        _clau_canvi(canvi): canvi
        for canvi in canvis
        if canvi["tipus"] == "servei_sense_cobertura"
    }
    claus = sorted(
        set(afectades) | set(proposades) | set(descobertes),
        key=lambda clau: (
            (afectades.get(clau) or proposades.get(clau) or descobertes[clau])["data"],
            (afectades.get(clau) or proposades.get(clau) or descobertes[clau]).get("torn") or "",
        ),
    )
    files = []
    for clau in claus:
        origen = afectades.get(clau)
        proposta = proposades.get(clau)
        descoberta = descobertes.get(clau)
        referencia = origen or proposta or descoberta
        pla_actual = (
            origen.get("treballador_nom") or str(origen.get("treballador_id"))
            if origen
            else "Descobert"
        )
        if proposta:
            pla_proposat = (
                proposta.get("treballador_nom")
                or str(proposta.get("treballador_id"))
            )
            estat = "Recuperada" if origen is None else "Reassignada"
        elif descoberta:
            pla_proposat = "Descobert"
            estat = "Continua descoberta" if origen is None else "Queda descoberta"
        else:
            pla_proposat = "Pendent de replanificació"
            estat = "Pendent"
        inici = referencia.get("hora_inici")
        fi = referencia.get("hora_fi")
        files.append(
            {
                "Necessitat": referencia.get("necessitat_id") or clau,
                "Data": pd.to_datetime(referencia["data"]),
                "Servei": referencia.get("torn") or "Sense servei",
                "Horari": f"{inici}–{fi}" if inici and fi else "—",
                "Zona": referencia.get("zona") or "—",
                "Pla actual": pla_actual,
                "Proposta": pla_proposat,
                "Estat": estat,
                "Durada (h)": referencia.get("durada_hores"),
            }
        )
    return pd.DataFrame(files)


def _dades_grafic_comparacio(comparacio: pd.DataFrame) -> pd.DataFrame:
    files = []
    for _, fila in comparacio.iterrows():
        etiqueta = f"{fila['Data'].strftime('%d/%m')} · {fila['Servei']}"
        files.extend(
            (
                {
                    "Servei i data": etiqueta,
                    "Etapa": "Pla publicat",
                    "Treballador": fila["Pla actual"],
                    "Situació": (
                        "Descobert" if fila["Pla actual"] == "Descobert"
                        else "Assignació afectada"
                    ),
                    "Horari": fila["Horari"],
                    "Zona": fila["Zona"],
                },
                {
                    "Servei i data": etiqueta,
                    "Etapa": "Proposta CP-SAT",
                    "Treballador": fila["Proposta"],
                    "Situació": (
                        "Descobert"
                        if fila["Proposta"] == "Descobert"
                        else "Cobert"
                        if fila["Estat"] in ("Recuperada", "Reassignada")
                        else "Pendent"
                    ),
                    "Horari": fila["Horari"],
                    "Zona": fila["Zona"],
                },
            )
        )
    return pd.DataFrame(files)


def _impacte_treballadors(comparacio: pd.DataFrame) -> pd.DataFrame:
    impacte: dict[str, dict] = {}

    def registre(nom: str) -> dict:
        return impacte.setdefault(
            nom,
            {
                "Treballador": nom,
                "Assignacions retirades": 0,
                "Assignacions afegides": 0,
                "Saldo hores": 0.0,
                "Dates afectades": set(),
            },
        )

    for _, fila in comparacio.iterrows():
        hores = float(fila["Durada (h)"] or 0)
        data = fila["Data"].strftime("%d/%m")
        origen = fila["Pla actual"]
        desti = fila["Proposta"]
        if origen != "Descobert" and origen != desti:
            item = registre(origen)
            item["Assignacions retirades"] += 1
            item["Saldo hores"] -= hores
            item["Dates afectades"].add(data)
        if desti not in ("Descobert", "Pendent de replanificació") and origen != desti:
            item = registre(desti)
            item["Assignacions afegides"] += 1
            item["Saldo hores"] += hores
            item["Dates afectades"].add(data)

    files = []
    for item in impacte.values():
        item = dict(item)
        item["Saldo hores"] = round(item["Saldo hores"], 2)
        item["Dates afectades"] = ", ".join(sorted(item["Dates afectades"]))
        files.append(item)
    return pd.DataFrame(files).sort_values("Treballador") if files else pd.DataFrame()


def _render_resum_proposta(detall: dict, comparacio: pd.DataFrame) -> None:
    resum = _resum_operatiu(detall)
    cobertura = (
        f"{resum['cobertes']}/{resum['total']}"
        if resum["total"] is not None
        else "No disponible"
    )
    col1, col2, col3, col4 = st.columns(4)
    col1.metric(
        "Cobertura proposada",
        cobertura,
        f"{resum['percentatge']:.1f}%" if resum["percentatge"] is not None else None,
        border=True,
    )
    col2.metric("Serveis descoberts", resum["descobertes"], border=True)
    col3.metric("Assignacions modificades", resum["afectades"], border=True)
    col4.metric("Cobertures proposades", resum["proposades"], border=True)

    errors = int(detall.get("errors_validacio") or 0)
    if errors:
        st.error(f"La proposta conté {errors} error(s) de validació i no es pot aprovar.")
    elif resum["descobertes"]:
        st.warning(
            f"La proposta és vàlida, però deixa {resum['descobertes']} "
            "servei(s) sense cobertura."
        )
    else:
        st.success("Proposta vàlida i sense serveis descoberts.")

    if comparacio.empty:
        return
    st.markdown("#### Abans i després")
    dades_grafic = _dades_grafic_comparacio(comparacio)
    ordre_serveis = list(dict.fromkeys(dades_grafic["Servei i data"]))
    figura = px.scatter(
        dades_grafic,
        x="Etapa",
        y="Servei i data",
        color="Situació",
        hover_name="Treballador",
        hover_data={"Horari": True, "Zona": True},
        category_orders={
            "Etapa": ["Pla publicat", "Proposta CP-SAT"],
            "Servei i data": list(reversed(ordre_serveis)),
        },
        color_discrete_map={
            "Cobert": "#2ca02c",
            "Descobert": "#d62728",
            "Assignació afectada": "#ff7f0e",
            "Pendent": "#7f7f7f",
        },
    )
    figura.update_traces(marker={"size": 16, "line": {"width": 1, "color": "white"}})
    figura.update_layout(
        height=min(650, max(280, 95 + 38 * len(comparacio))),
        margin={"l": 10, "r": 10, "t": 15, "b": 10},
        legend_title_text="Situació",
        xaxis_title=None,
        yaxis_title=None,
    )
    st.plotly_chart(figura, width="stretch")

    impacte = _impacte_treballadors(comparacio)
    if not impacte.empty:
        st.markdown("#### Impacte sobre els treballadors")
        st.dataframe(impacte, width="stretch", hide_index=True)

    with st.expander("Veure el detall complet dels canvis"):
        st.dataframe(
            comparacio,
            width="stretch",
            hide_index=True,
            column_config={
                "Data": st.column_config.DateColumn("Data", format="DD/MM/YYYY"),
                "Durada (h)": st.column_config.NumberColumn(
                    "Durada (h)", format="%.2f h"
                ),
            },
        )


def render_pestanya_incidencies(db_path: str | Path) -> None:
    if st.session_state.pop("reinicia_registre_incidencia", False):
        _reinicia_registre_incidencia()
    ruta = str(db_path)
    inicialitza_planificacio(ruta)
    treballadors = llista_treballadors(ruta)
    st.header("Incidències")
    st.caption(
        "Registra la incidència, genera una proposta de cobertura i revisa-la "
        "abans d'aplicar-la al pla."
    )
    registrar, incidencies, propostes = st.tabs(
        ["1. Registrar", "2. Generar proposta", "3. Revisar i aplicar"]
    )
    with registrar:
        st.caption("Indica què ha passat i a quin treballador afecta.")
        missatge = st.session_state.pop("missatge_incidencia_registrada", None)
        if missatge:
            st.success(missatge)
        treballador = selector_treballador(
            treballadors, "incidencia_treballador", "Treballador"
        )
        tipus = st.selectbox(
            "Tipus", TIPUS_INCIDENCIA,
            format_func=lambda x: ETIQUETES_TIPUS[x], key="incidencia_tipus",
        )
        substitut = None
        if tipus == "substitucio":
            substitut = selector_treballador(
                treballadors, "incidencia_substitut", "Treballador substitut"
            )
        col1, col2, col3 = st.columns(3)
        with col1:
            comunicacio = st.date_input(
                "Data de comunicació", value=date.today(), key="incidencia_comunicacio"
            )
        with col2:
            inici = st.date_input(
                "Data d'alta" if tipus == "alta_anticipada" else "Inici",
                value=None, key="incidencia_inici",
            )
        with col3:
            fi = inici if tipus == "alta_anticipada" else st.date_input(
                "Final", value=None, key="incidencia_fi"
            )
        motiu = st.text_input(
            "Motiu o observacions (opcional)", key="incidencia_motiu"
        )
        confirma = st.checkbox(
            "Confirmo el registre de la incidència", key="incidencia_confirma"
        )
        enviar = st.button(
            "Registrar incidència", type="primary", key="boto_registra_incidencia"
        )
        if enviar:
            if treballador is None or inici is None or fi is None or (tipus == "substitucio" and substitut is None):
                st.error("Selecciona el treballador i les dates de la incidència.")
            elif not confirma:
                st.error("Cal confirmar el registre.")
            else:
                try:
                    incidencia_id = registrar_incidencia(
                        ruta, treballador["id"], tipus, comunicacio, inici, fi, motiu,
                        substitut["id"] if substitut else None,
                    )
                    st.session_state["missatge_incidencia_registrada"] = (
                        f"Incidència #{incidencia_id} registrada. Ara pots generar-ne una proposta."
                    )
                    st.session_state["reinicia_registre_incidencia"] = True
                    st.rerun()
                except ValueError as error: st.error(str(error))
    with incidencies:
        files = llista_incidencies(ruta)
        if not files: st.info("No hi ha incidències registrades.")
        else:
            st.caption(
                "Selecciona una incidència registrada per buscar-ne la millor "
                "cobertura possible."
            )
            st.dataframe(
                pd.DataFrame(files),
                width="stretch",
                hide_index=True,
            )
            obertes = [f for f in files if f["estat"] in ("registrada", "en_proposta")]
            incidencia = st.selectbox(
                "Incidència per preparar",
                obertes,
                format_func=lambda x: (
                    f"#{x['id']} · {ETIQUETES_TIPUS[x['tipus']]} · "
                    f"{x['treballador']} · {x['data_inici']} a {x['data_fi']}"
                ),
                index=None,
                placeholder="Selecciona una incidència",
            )
            if incidencia and st.button(
                "Generar proposta CP-SAT",
                type="primary",
                icon=":material/auto_fix_high:",
            ):
                proposta = generar_proposta(ruta, incidencia["id"])
                st.success(f"Proposta #{proposta['id']} generada sense modificar el pla publicat.")
                st.rerun()
    with propostes:
        files = llista_propostes(ruta)
        if not files: st.info("Encara no hi ha propostes.")
        else:
            st.caption(
                "Comprova la cobertura i els canvis abans d'aprovar una "
                "proposta."
            )
            resum_propostes = pd.DataFrame(files)
            columnes_resum = [
                columna
                for columna in (
                    "id", "tipus", "treballador", "estat", "data_inici",
                    "data_fi", "total_assignacions_afectades",
                    "total_assignacions_proposades", "total_sense_cobertura",
                )
                if columna in resum_propostes.columns
            ]
            st.dataframe(
                resum_propostes[columnes_resum],
                width="stretch",
                hide_index=True,
            )
            proposta = st.selectbox(
                "Proposta a revisar",
                files,
                format_func=lambda x: (
                    f"#{x['id']} · {ETIQUETES_TIPUS[x['tipus']]} · "
                    f"{x['treballador']} · {x['estat']}"
                ),
                index=None,
                placeholder="Selecciona una proposta",
            )
            if proposta:
                detall = obtenir_proposta(ruta, proposta["id"])
                candidats = [
                    canvi for canvi in detall["canvis"]
                    if canvi["tipus"] == "candidat_cobertura"
                ]
                comparacio = _comparacio_operativa(detall["canvis"])
                st.subheader("Resum operatiu de la proposta")
                _render_resum_proposta(detall, comparacio)
                if comparacio.empty:
                    st.info(
                        "No hi ha assignacions afectades ni cobertures noves "
                        "dins del període."
                    )
                if candidats and detall.get("motor") != "cp_sat":
                    with st.expander("Veure les opcions del detector anterior"):
                        st.dataframe(
                            pd.DataFrame(candidats)[
                                [
                                    "data", "torn", "assignacio_id",
                                    "treballador_id", "descripcio",
                                ]
                            ],
                            width="stretch",
                            hide_index=True,
                        )
                if detall.get("motor") == "cp_sat":
                    st.caption(
                        "En aprovar, es comprovarà que el pla no hagi canviat "
                        "i les anul·lacions i cobertures es publicaran en una "
                        "única transacció."
                    )
                else:
                    st.caption(
                        "En aprovar, les assignacions afectades queden anul·lades. "
                        "Les opcions mostrades serveixen per decidir la cobertura en "
                        "la replanificació posterior."
                    )
                if proposta["estat"] == "esborrany":
                    confirma = st.checkbox(
                        "Confirmo que he revisat la proposta",
                        key=f"aprova_{proposta['id']}",
                    )
                    if st.button(
                        "Aprovar i aplicar proposta",
                        type="primary",
                        icon=":material/check_circle:",
                        key=f"boto_aprova_{proposta['id']}",
                    ):
                        if not confirma: st.error("Cal confirmar l'aprovació.")
                        else:
                            try:
                                resultat = aprovar_proposta(ruta, proposta["id"])
                                missatge = (
                                    f"Proposta aprovada: {resultat['dies_incidencia']} dies aplicats "
                                    f"i {resultat['assignacions_anullades']} assignacions anul·lades."
                                )
                                if resultat["dies_substitut_activats"]:
                                    missatge += (
                                        f" S'han convertit en dies de treball "
                                        f"{resultat['dies_substitut_activats']} descansos base del substitut."
                                    )
                                if resultat.get("assignacions_publicades"):
                                    missatge += (
                                        f" S'han publicat "
                                        f"{resultat['assignacions_publicades']} "
                                        "cobertures noves."
                                    )
                                if resultat.get("serveis_descoberts"):
                                    missatge += (
                                        f" Queden {resultat['serveis_descoberts']} "
                                        "serveis descoberts."
                                    )
                                st.success(missatge)
                                st.rerun()
                            except ValueError as error:
                                st.error(str(error))
