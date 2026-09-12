"""Component compartit del workflow manual de notificacions Xivato."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

from planificador_cp_sat.services.notificacions_xivato import (
    XivatoError,
    list_xivato_notifications,
    load_pilot_worker_ids,
    load_xivato_settings,
    prepare_xivato_notifications,
    preview_xivato_notifications,
    send_xivato_notifications,
)


def _notice_key(publication_id: int) -> str:
    return f"xivato_notice_{publication_id}"


def _confirmation_key(publication_id: int) -> str:
    return f"xivato_confirm_{publication_id}"


def _reset_confirmation_on_rerun(publication_id: int) -> None:
    st.session_state[f"{_confirmation_key(publication_id)}_reset"] = True


def _apply_pending_confirmation_reset(publication_id: int) -> None:
    confirmation_key = _confirmation_key(publication_id)
    if st.session_state.pop(f"{confirmation_key}_reset", False):
        st.session_state.pop(confirmation_key, None)


def render_xivato_pilot(db_path: str | Path, audit) -> None:
    """Mostra el flux manual preparar -> comprovar -> enviar."""
    if audit.reverted_at is not None:
        return

    publication_id = audit.publication_id
    _apply_pending_confirmation_reset(publication_id)
    with st.expander("Notificacions Xivato · pilot", expanded=True):
        notice = st.session_state.pop(_notice_key(publication_id), None)
        if notice:
            st.success(notice)

        try:
            pilot_worker_ids = load_pilot_worker_ids()
        except XivatoError as error:
            st.info(str(error))
            return

        st.caption(
            "Pilot limitat als treballadors "
            + " i ".join(pilot_worker_ids)
            + ". Cap altre destinatari es pot incorporar a aquesta cua."
        )
        notices = list_xivato_notifications(db_path, publication_id)
        if not notices:
            if st.button(
                "Preparar avisos Xivato",
                icon=":material/playlist_add_check:",
                key=f"xivato_prepare_{publication_id}",
            ):
                try:
                    result = prepare_xivato_notifications(
                        db_path,
                        publication_id,
                        pilot_worker_ids,
                    )
                    st.session_state[_notice_key(publication_id)] = (
                        f"Xivato: {result['creats']} avisos preparats "
                        "sense enviar."
                    )
                    st.rerun()
                except (sqlite3.Error, ValueError, XivatoError) as error:
                    st.error(str(error))
            return

        frame = pd.DataFrame(
            [
                {
                    "Treballador": item["treballador"],
                    "Identificador": item["treballador_id"],
                    "Data": item["data"],
                    "Avís": item["tipus"],
                    "Estat": item["estat"],
                    "Intents": item["intents"],
                    "Error": item["error"] or "",
                }
                for item in notices
            ]
        )
        st.dataframe(frame, hide_index=True, width="stretch")

        try:
            settings = load_xivato_settings()
        except XivatoError as error:
            settings = None
            st.info(str(error))

        previewable = any(
            item["estat"] in {"pendent", "sense_mapping", "error"}
            for item in notices
        )
        if st.button(
            "Comprovar destinataris",
            icon=":material/preview:",
            disabled=not previewable or settings is None,
            key=f"xivato_preview_{publication_id}",
        ):
            result = preview_xivato_notifications(
                db_path,
                publication_id,
                settings,
            )
            st.session_state[_notice_key(publication_id)] = (
                f"Xivato: {result['mapejats']} avisos amb destinatari; "
                f"{result['sense_mapping']} sense mapping; "
                f"{result['errors']} errors."
            )
            st.rerun()

        sendable = sum(
            item["estat"] == "previsualitzat" for item in notices
        )
        confirmed = st.checkbox(
            f"Confirmo l'enviament dels {sendable} avisos previsualitzats",
            disabled=sendable == 0,
            key=_confirmation_key(publication_id),
        )
        if st.button(
            "Enviar avisos Xivato",
            type="primary",
            icon=":material/send:",
            disabled=(not confirmed or sendable == 0 or settings is None),
            key=f"xivato_send_{publication_id}",
        ):
            result = send_xivato_notifications(
                db_path,
                publication_id,
                settings,
            )
            st.session_state[_notice_key(publication_id)] = (
                f"Xivato: {result['enviats']} processats; "
                f"{result['duplicats']} duplicats; "
                f"{result['errors']} errors."
            )
            _reset_confirmation_on_rerun(publication_id)
            st.rerun()
