"""Pestanya Streamlit per gestionar descansos, baixes i substitucions."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from ui_components import claus_selector_treballador, selector_treballador

from servei_descansos import (
    ORIGENS_EDITABLES,
    afegir_periode,
    crear_substitucio,
    disponibilitat_dia,
    detectar_serveis_descoberts,
    eliminar_periode,
    eliminar_substitucio,
    historial_canvis,
    llista_substitucions,
    llista_treballadors,
    moviments_treballador,
    resum_mensual,
    estadistiques_descansos,
    alertes_baixes_pendents,
)


ETIQUETES_ORIGEN = {
    "manual": "Descans puntual/manual",
    "temporal": "Període temporal",
    "baixa": "Baixa",
}


# Camps que es poden reiniciar des del botó general d'actualització.  Les claus
# explícites eviten que Streamlit conservi una selecció antiga en recarregar.
CLAUS_FORMULARIS = (
    *claus_selector_treballador("descansos_treballador_consulta"), "consulta_periode",
    "consulta_dia_moviments", "consulta_mes_moviments", "consulta_any_moviments",
    "descansos_treballador_edicio", "descansos_operacio", "descans_origen",
    "descans_data_inici", "descans_data_fi", "descans_motiu", "descans_confirma",
    "substitucio_original", "substitucio_substitut", "substitucio_data_inici",
    "substitucio_data_fi", "substitucio_motiu", "substitucio_permet_conflictes",
    "substitucio_confirma", "substitucio_eliminar", "substitucio_eliminar_confirma",
    "disponibilitat_dia", "serveis_descoberts_inici", "serveis_descoberts_fi",
)


def reinicia_formularis_descansos() -> None:
    """Elimina l'estat dels camps de descansos abans de tornar a renderitzar."""
    for clau in CLAUS_FORMULARIS:
        st.session_state.pop(clau, None)


def _etiqueta_treballador(treballador: dict) -> str:
    return (
        f"{treballador['treballador']} · {treballador['plaza']} "
        f"(ID {treballador['id']})"
    )


def _mostra_resultat_operacio(resultat: dict, accio: str) -> None:
    afegits = resultat.get("afegits", 0)
    existents = resultat.get("existents", 0)
    if afegits:
        st.success(f"{accio}: {afegits} dia/dies actualitzat/s correctament.")
    if existents:
        st.info(f"{existents} dia/dies ja existien i no s'han modificat.")


def _seccio_consulta(db_path: str, treballadors: list[dict]) -> None:
    st.subheader("Consulta de treballador")
    treballador = selector_treballador(
        treballadors,
        clau="descansos_treballador_consulta",
    )
    if treballador is None:
        st.info("Selecciona un treballador per consultar-ne els descansos.")
        return

    tipus_periode = st.segmented_control(
        "Període de consulta", ["Tots", "Dia", "Mes", "Any"],
        default="Tots",
        key="consulta_periode",
    )
    any_consulta = None
    prefix_data = None
    if tipus_periode == "Dia":
        dia = st.date_input("Dia", value=None, key="consulta_dia_moviments")
        if dia is None:
            st.info("Indica el dia que vols consultar.")
            return
        any_consulta = dia.year
        prefix_data = dia.isoformat()
    elif tipus_periode == "Mes":
        mes_text = st.text_input(
            "Mes (YYYY/MM)", key="consulta_mes_moviments", placeholder="2025/11",
        ).strip()
        if not mes_text:
            st.info("Indica el mes amb el format YYYY/MM.")
            return
        try:
            any_text, mes_num_text = mes_text.split("/", maxsplit=1)
            if len(any_text) != 4 or len(mes_num_text) != 2:
                raise ValueError
            any_consulta, mes_num = int(any_text), int(mes_num_text)
            if not 1 <= mes_num <= 12:
                raise ValueError
            prefix_data = f"{any_consulta}-{mes_num:02d}"
        except ValueError:
            st.error("El mes ha de tenir el format YYYY/MM, per exemple 2025/11.")
            return
    elif tipus_periode == "Any":
        any_consulta = st.number_input(
            "Any", min_value=2000, max_value=2100, value=None, step=1,
            key="consulta_any_moviments", placeholder="Per exemple, 2026",
        )
        if any_consulta is None:
            st.info("Indica l'any que vols consultar.")
            return

    moviments = moviments_treballador(db_path, treballador["id"], any_consulta)
    if prefix_data:
        moviments = [moviment for moviment in moviments if moviment["data"].startswith(prefix_data)]
    if moviments:
        df = pd.DataFrame(moviments)
        st.dataframe(
            df[["data", "rol", "origen", "motiu", "nom_original", "nom_substitut"]],
            width="stretch", hide_index=True,
            column_config={
                "data": "Data", "rol": "Rol", "origen": "Origen",
                "motiu": "Motiu", "nom_original": "Treballador original",
                "nom_substitut": "Substitut",
            },
        )
    else:
        st.info("No hi ha descansos, baixes ni substitucions en aquest període.")


def _seccio_descansos(db_path: str, treballadors: list[dict]) -> None:
    st.subheader("Afegir o eliminar descansos i baixes")
    st.caption("Les eliminacions només afecten registres manuals, temporals o de baixa; no substitucions.")
    treballador = st.selectbox(
        "Treballador afectat", treballadors, format_func=_etiqueta_treballador,
        key="descansos_treballador_edicio", index=None,
        placeholder="Selecciona un treballador",
    )
    if treballador is None:
        st.info("Selecciona un treballador abans de gestionar-ne els descansos.")
        return
    operacio = st.segmented_control(
        "Operació", ["Afegir", "Eliminar"], default="Afegir",
        key="descansos_operacio",
    )
    with st.form("formulari_descansos", clear_on_submit=True):
        origen = st.selectbox(
            "Tipus", list(ORIGENS_EDITABLES),
            format_func=lambda valor: ETIQUETES_ORIGEN[valor],
            key="descans_origen",
        )
        col_inici, col_fi = st.columns(2)
        with col_inici:
            data_inici = st.date_input("Data d'inici", value=None, key="descans_data_inici")
        with col_fi:
            data_fi = st.date_input("Data final", value=None, key="descans_data_fi")
        motiu = st.text_input(
            "Motiu" + (" (obligatori per a baixa)" if origen == "baixa" else " (opcional)"),
            key="descans_motiu",
        )
        confirma = st.checkbox(
            f"Confirmo que vull {operacio.lower()} aquest període a la base de dades.",
            key="descans_confirma",
        )
        envia = st.form_submit_button(f"{operacio} període", type="primary")

    if not envia:
        return
    if not confirma:
        st.error("Cal confirmar l'operació abans de continuar.")
        return
    if data_inici is None or data_fi is None:
        st.error("Indica la data d'inici i la data final.")
        return
    if data_fi < data_inici:
        st.error("La data final no pot ser anterior a la inicial.")
        return
    if origen == "baixa" and operacio == "Afegir" and not motiu.strip():
        st.error("Indica el motiu de la baixa.")
        return
    try:
        if operacio == "Afegir":
            resultat = afegir_periode(
                db_path, treballador["id"], data_inici, data_fi, origen, motiu
            )
            _mostra_resultat_operacio(resultat, "Període afegit")
        else:
            eliminats = eliminar_periode(
                db_path, treballador["id"], data_inici, data_fi, origen
            )
            st.success(f"Període eliminat: {eliminats} dia/dies eliminat/s.")
        st.cache_data.clear()
    except (ValueError, FileNotFoundError) as error:
        st.error(str(error))


def _seccio_substitucions(db_path: str, treballadors: list[dict]) -> None:
    st.subheader("Substitucions")
    crear, eliminar, consultar = st.tabs(["Crear", "Eliminar", "Actives i futures"])

    with crear:
        with st.form("formulari_substitucio", clear_on_submit=True):
            original = st.selectbox(
                "Treballador original", treballadors, format_func=_etiqueta_treballador,
                key="substitucio_original", index=None,
                placeholder="Selecciona el treballador original",
            )
            substitut = st.selectbox(
                "Treballador substitut", treballadors, format_func=_etiqueta_treballador,
                key="substitucio_substitut", index=None,
                placeholder="Selecciona el treballador substitut",
            )
            col_inici, col_fi = st.columns(2)
            with col_inici:
                data_inici = st.date_input("Inici de la substitució", value=None, key="substitucio_data_inici")
            with col_fi:
                data_fi = st.date_input("Final de la substitució", value=None, key="substitucio_data_fi")
            motiu = st.text_input("Motiu de la substitució (opcional)", key="substitucio_motiu")
            permet_conflictes = st.checkbox(
                "Permeto substituir encara que el substitut tingui una incidència o un descans no base",
                key="substitucio_permet_conflictes",
                help=(
                    "Els descansos base s'ajusten automàticament perquè el substitut "
                    "consti com a treballant. Aquesta opció només força altres conflictes."
                ),
            )
            confirma = st.checkbox("Confirmo la creació de la substitució", key="substitucio_confirma")
            envia = st.form_submit_button("Crear substitució", type="primary")

        if envia:
            if original is None or substitut is None or data_inici is None or data_fi is None:
                st.error("Selecciona els dos treballadors i les dates de la substitució.")
            elif not confirma:
                st.error("Cal confirmar la substitució.")
            elif original["id"] == substitut["id"]:
                st.error("El treballador original i el substitut han de ser diferents.")
            else:
                try:
                    resultat = crear_substitucio(
                        db_path, original["id"], substitut["id"], data_inici, data_fi,
                        motiu, permet_conflictes,
                    )
                    if resultat["conflictes"] and not permet_conflictes:
                        st.warning(
                            "El substitut té una incidència, un descans no base o una altra "
                            "cobertura en aquest període. Revisa-ho abans de forçar la substitució."
                        )
                        st.dataframe(pd.DataFrame(resultat["conflictes"]), hide_index=True)
                    else:
                        _mostra_resultat_operacio(resultat, "Substitució creada")
                        if resultat["descansos_substitut_retirats"]:
                            st.info(
                                "S'han convertit en dies de treball "
                                f"{resultat['descansos_substitut_retirats']} descansos "
                                "registrats del substitut."
                            )
                        st.cache_data.clear()
                except (ValueError, FileNotFoundError) as error:
                    st.error(str(error))

    with eliminar:
        substitucions = llista_substitucions(db_path)
        if not substitucions:
            st.info("No hi ha substitucions registrades.")
        else:
            seleccionada = st.selectbox(
                "Substitució a eliminar", substitucions,
                format_func=lambda fila: (
                    f"{fila['original']} → {fila['substitut']} | "
                    f"{fila['data_inici']} a {fila['data_fi']}"
                ),
                key="substitucio_eliminar", index=None,
                placeholder="Selecciona una substitució",
            )
            if seleccionada is None:
                st.info("Selecciona una substitució per poder eliminar-la.")
            else:
                st.warning("Aquesta acció eliminarà tots els dies del període de substitució seleccionat.")
                confirma = st.checkbox("Confirmo l'eliminació d'aquesta substitució", key="substitucio_eliminar_confirma")
                if st.button("Eliminar substitució", type="primary", key="boto_eliminar_substitucio"):
                    if not confirma:
                        st.error("Cal confirmar l'eliminació.")
                    else:
                        eliminats = eliminar_substitucio(
                            db_path, seleccionada["original_id"], seleccionada["substitut_id"],
                            seleccionada["data_inici"], seleccionada["data_fi"], seleccionada["motiu"],
                        )
                        st.success(
                            f"Substitució eliminada: {eliminats} dia/dies eliminat/s. "
                            "S'han restaurat els calendaris previs dels dos treballadors."
                        )
                        st.cache_data.clear()

    with consultar:
        substitucions = llista_substitucions(db_path)
        if substitucions:
            st.dataframe(
                pd.DataFrame(substitucions),
                width="stretch",
                hide_index=True,
            )
        else:
            st.info("No hi ha substitucions actives o futures.")


def _seccio_disponibilitat(db_path: str) -> None:
    st.subheader("Disponibilitat i seguiment")
    view = st.selectbox(
        "Què vols consultar?",
        (
            "Disponibilitat d'un dia",
            "Calendari mensual",
            "Serveis descoberts",
            "Estadístiques",
            "Historial de canvis",
            "Alertes de baixes",
        ),
        key="consulta_disponibilitat_tipus",
    )

    if view == "Disponibilitat d'un dia":
        dia = st.date_input("Data a consultar", value=None, key="disponibilitat_dia")
        if dia is None:
            st.caption("Indica una data per veure qui està disponible.")
        else:
            dades = disponibilitat_dia(db_path, dia)
            if not dades["descansos"]:
                st.warning(
                    "No hi ha descansos, baixes ni substitucions registrats per a aquesta data. "
                    "Per això tots els treballadors apareixen com a disponibles."
                )
            col_disponibles, col_descansos = st.columns(2)
            with col_disponibles:
                st.metric(
                    "Disponibles",
                    len(dades["disponibles"]),
                    border=True,
                )
                st.dataframe(
                    pd.DataFrame(dades["disponibles"]),
                    width="stretch",
                    hide_index=True,
                )
            with col_descansos:
                st.metric(
                    "No disponibles",
                    len(dades["descansos"]),
                    border=True,
                )
                st.dataframe(
                    pd.DataFrame(dades["descansos"]),
                    width="stretch",
                    hide_index=True,
                )

    elif view == "Calendari mensual":
        col_any, col_mes = st.columns(2)
        with col_any:
            any_ = st.number_input("Any", 2020, 2100, date.today().year, key="calendari_any")
        with col_mes:
            mes = st.number_input("Mes", 1, 12, date.today().month, key="calendari_mes")
        resum = resum_mensual(db_path, int(any_), int(mes))
        df = pd.DataFrame(resum)
        st.bar_chart(df.set_index("data")["places_no_disponibles"])
        st.dataframe(df, width="stretch", hide_index=True)

    elif view == "Serveis descoberts":
        col_inici, col_fi = st.columns(2)
        with col_inici:
            data_inici = st.date_input("Data d'inici", value=None, key="serveis_descoberts_inici")
        with col_fi:
            data_fi = st.date_input("Data final", value=None, key="serveis_descoberts_fi")
        if st.button("Analitzar serveis descoberts", type="primary"):
            if data_inici is None or data_fi is None:
                st.error("Indica la data d'inici i la data final.")
            else:
                try:
                    descoberts = detectar_serveis_descoberts(db_path, data_inici, data_fi)
                    if descoberts:
                        st.warning(f"S'han detectat {len(descoberts)} servei/s sense cobertura base.")
                        st.dataframe(
                            pd.DataFrame(descoberts),
                            width="stretch",
                            hide_index=True,
                        )
                    else:
                        st.success("No s'han detectat serveis descoberts en aquest període.")
                except ValueError as error:
                    st.error(str(error))

    elif view == "Estadístiques":
        any_ = st.number_input("Any (0 per a tots)", 0, 2100, 0, step=1, key="estadistiques_descansos_any")
        dades = estadistiques_descansos(db_path, int(any_) if any_ else None)
        df = pd.DataFrame(dades)
        total = int(df["total_descansos"].sum()) if not df.empty else 0
        col_total, col_mitjana = st.columns(2)
        col_total.metric("Dies de descans / baixa", total, border=True)
        col_mitjana.metric(
            "Mitjana per treballador",
            f"{total / len(df):.1f}" if len(df) else "0",
            border=True,
        )
        st.bar_chart(df.head(15).set_index("treballador")["total_descansos"])
        st.dataframe(df, width="stretch", hide_index=True)

    elif view == "Historial de canvis":
        canvis = historial_canvis(db_path)
        if canvis:
            st.dataframe(
                pd.DataFrame(canvis),
                width="stretch",
                hide_index=True,
            )
        else:
            st.info("No hi ha canvis manuals, temporals, baixes o substitucions.")

    else:
        dies_marge = st.number_input("Dies d'avís", 0, 90, 7, key="alertes_baixes_marge")
        dades = alertes_baixes_pendents(db_path, int(dies_marge))
        if dades:
            df = pd.DataFrame(dades)
            st.warning(f"Hi ha {len(df)} baixa/baixes que requereixen revisió.")
            st.dataframe(df, width="stretch", hide_index=True)
        else:
            st.success("No hi ha baixes expirades ni properes a finalitzar.")


def render_pestanya_descansos(db_path: str | Path) -> None:
    """Renderitza la pantalla completa de descansos dins de l'aplicació principal."""
    ruta_db = str(db_path)
    st.header("Consulta")
    st.caption(
        "Consulta calendaris, disponibilitat i serveis descoberts. Les "
        "incidències es registren des de la pantalla Incidències."
    )
    if st.button(
        "Netejar camps",
        icon=":material/refresh:",
        type="tertiary",
        key="reinicia_descansos",
    ):
        reinicia_formularis_descansos()
        st.rerun()

    try:
        treballadors = llista_treballadors(ruta_db)
    except FileNotFoundError as error:
        st.error(str(error))
        return

    consulta, disponibilitat = st.tabs(
        ["Per treballador", "Disponibilitat i seguiment"]
    )
    with consulta:
        _seccio_consulta(ruta_db, treballadors)
    with disponibilitat:
        _seccio_disponibilitat(ruta_db)
