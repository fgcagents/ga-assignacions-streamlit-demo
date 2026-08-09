"""Components Streamlit compartits per les pantalles de gestió."""

from __future__ import annotations

import unicodedata

import streamlit as st


def _normalitza_text(valor: object) -> str:
    text = unicodedata.normalize("NFKD", str(valor).casefold())
    return "".join(caracter for caracter in text if not unicodedata.combining(caracter))


def _etiqueta_treballador(treballador: dict) -> str:
    return (
        f"{treballador['treballador']} · {treballador['plaza']} "
        f"(ID {treballador['id']})"
    )


def _selecciona_treballador(clau_seleccio: str, treballador_id: object) -> None:
    st.session_state[clau_seleccio] = treballador_id


def _neteja_selector(clau_cerca: str, clau_seleccio: str) -> None:
    st.session_state.pop(clau_cerca, None)
    st.session_state.pop(clau_seleccio, None)


def claus_selector_treballador(clau: str) -> tuple[str, str]:
    return f"{clau}_cerca", f"{clau}_seleccionat"


def selector_treballador(
    treballadors: list[dict],
    clau: str,
    etiqueta: str = "Treballador",
    maxim_resultats: int = 15,
) -> dict | None:
    """Cercador ordenat amb resultats visibles sempre sota la caixa."""
    clau_cerca, clau_seleccio = claus_selector_treballador(clau)
    cerca = st.text_input(
        etiqueta,
        key=clau_cerca,
        placeholder="Escriu el nom, la plaça o l'ID",
    )

    seleccionat_id = st.session_state.get(clau_seleccio)
    seleccionat = next(
        (fila for fila in treballadors if str(fila["id"]) == str(seleccionat_id)),
        None,
    )
    if seleccionat is not None:
        col_text, col_boto = st.columns([5, 1])
        col_text.success(f"Seleccionat: {_etiqueta_treballador(seleccionat)}")
        col_boto.button(
            "Canviar",
            key=f"{clau}_canviar",
            on_click=_neteja_selector,
            args=(clau_cerca, clau_seleccio),
            use_container_width=True,
        )
        return seleccionat

    terme = _normalitza_text(cerca.strip())
    if not terme:
        st.caption("Les coincidències apareixeran aquí sota mentre escrius.")
        return None

    coincidencies = [
        treballador for treballador in treballadors
        if terme in _normalitza_text(_etiqueta_treballador(treballador))
    ]
    if not coincidencies:
        st.warning("No s'han trobat treballadors amb aquest text consecutiu.")
        return None

    st.caption(f"{len(coincidencies)} coincidència/es. Selecciona un treballador:")
    for treballador in coincidencies[:maxim_resultats]:
        st.button(
            _etiqueta_treballador(treballador),
            key=f"{clau}_opcio_{treballador['id']}",
            on_click=_selecciona_treballador,
            args=(clau_seleccio, treballador["id"]),
            use_container_width=True,
        )
    if len(coincidencies) > maxim_resultats:
        st.caption(
            f"Es mostren els primers {maxim_resultats}. Escriu més lletres per reduir la llista."
        )
    return None
