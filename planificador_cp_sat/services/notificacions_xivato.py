"""Cua auditable i client HTTPS per al pilot de Xivato."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from planificador_cp_sat.services.esquema_planificacio import (
    migrate_planning_schema,
)


DEFAULT_API_URL = "https://notificacions-xivato.web.app/api/notificacions"
PILOT_WORKER_IDS_ENV = "XIVATO_PILOT_WORKER_IDS"
API_KEY_ENV = "XIVATO_API_KEY"
API_URL_ENV = "XIVATO_API_URL"
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
PREVIEWABLE_STATES = frozenset({"pendent", "sense_mapping", "error"})


class XivatoError(RuntimeError):
    """Error controlat de configuració, preparació o comunicació."""


@dataclass(frozen=True, slots=True)
class XivatoSettings:
    api_url: str
    api_key: str


Sender = Callable[[dict, XivatoSettings], dict]


def load_pilot_worker_ids(
    environment: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Llegeix els dos únics treballadors autoritzats durant el pilot."""
    values = environment if environment is not None else os.environ
    raw_value = values.get(PILOT_WORKER_IDS_ENV, "")
    worker_ids = tuple(
        dict.fromkeys(item.strip() for item in raw_value.split(",") if item.strip())
    )
    if len(worker_ids) != 2:
        raise XivatoError(
            f"{PILOT_WORKER_IDS_ENV} ha de contenir exactament dos identificadors"
        )
    if not all(IDENTIFIER_PATTERN.fullmatch(item) for item in worker_ids):
        raise XivatoError("Els identificadors pilot contenen caràcters no admesos")
    return worker_ids[0], worker_ids[1]


def load_xivato_settings(
    environment: Mapping[str, str] | None = None,
) -> XivatoSettings:
    """Carrega l'endpoint públic i la clau privada sense valors al repositori."""
    values = environment if environment is not None else os.environ
    api_url = values.get(API_URL_ENV, DEFAULT_API_URL).strip()
    parsed = urlparse(api_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise XivatoError(f"{API_URL_ENV} ha de ser un URL HTTPS")
    api_key = values.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise XivatoError(f"Falta la variable privada {API_KEY_ENV}")
    return XivatoSettings(api_url=api_url, api_key=api_key)


def _read_json_response(response) -> dict:
    payload = response.read().decode("utf-8")
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise XivatoError("Xivato ha retornat una resposta no vàlida") from error
    if not isinstance(value, dict):
        raise XivatoError("Xivato ha retornat una resposta inesperada")
    return value


def post_xivato(
    payload: dict,
    settings: XivatoSettings,
    *,
    timeout_seconds: float = 10,
) -> dict:
    """Envia una petició JSON sense registrar ni exposar la clau privada."""
    request = Request(
        settings.api_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": settings.api_key,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return _read_json_response(response)
    except HTTPError as error:
        try:
            detail = _read_json_response(error).get("error", "")
        except XivatoError:
            detail = ""
        suffix = f": {detail}" if detail else ""
        raise XivatoError(f"Xivato ha respost HTTP {error.code}{suffix}") from error
    except (URLError, TimeoutError) as error:
        raise XivatoError("No s'ha pogut contactar amb Xivato") from error


def _display_date(raw_value: str) -> str:
    return date.fromisoformat(raw_value).strftime("%d/%m/%Y")


def _publication_context(
    connection: sqlite3.Connection,
    publication_id: int,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT p.id, p.execucio_id, p.data_inici, p.data_fi, p.reverted_at,
               v.versio
        FROM publicacions_planificacio_cp_sat p
        JOIN versions_pla_publicat v
          ON v.publicacio_id = p.id AND v.tipus_event = 'publicacio'
        WHERE p.id = ?
        """,
        (publication_id,),
    ).fetchone()
    if row is None:
        raise XivatoError("La publicació no existeix o no té versió oficial")
    if row["reverted_at"] is not None:
        raise XivatoError("No es poden preparar avisos d'una publicació revertida")
    return row


def _event_id(
    publication_id: int,
    plan_version: int,
    change_id: int,
    worker_id: str,
    action: str,
) -> str:
    return (
        f"cp-sat:p{publication_id}:v{plan_version}:"
        f"c{change_id}:w{worker_id}:{action}"
    )


def _insert_notice(
    connection: sqlite3.Connection,
    *,
    publication_id: int,
    plan_version: int,
    change_id: int,
    worker_id: str,
    action: str,
    notification_type: str,
    variables: dict,
) -> int:
    event_id = _event_id(
        publication_id,
        plan_version,
        change_id,
        worker_id,
        action,
    )
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO notificacions_xivato
        (publicacio_id, versio_pla, canvi_id, treballador_id,
         event_id, tipus, variables_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            publication_id,
            plan_version,
            change_id,
            worker_id,
            event_id,
            notification_type,
            json.dumps(variables, ensure_ascii=False, sort_keys=True),
        ),
    )
    return int(cursor.rowcount == 1)


def prepare_xivato_notifications(
    database_path: str | Path,
    publication_id: int,
    pilot_worker_ids: tuple[str, str],
) -> dict:
    """Crea la cua només per als dos pilots afectats pel bloc publicat."""
    migrate_planning_schema(database_path)
    allowed = frozenset(pilot_worker_ids)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            publication = _publication_context(connection, publication_id)
            changes = connection.execute(
                """
                SELECT id, tipus, data, servei, assignacio_nova_id,
                       anterior_json, posterior_json
                FROM canvis_planificacio_cp_sat
                WHERE execucio_id = ? AND data BETWEEN ? AND ?
                ORDER BY ordre
                """,
                (
                    publication["execucio_id"],
                    publication["data_inici"],
                    publication["data_fi"],
                ),
            ).fetchall()
            inserted = 0
            for change in changes:
                previous = (
                    json.loads(change["anterior_json"])
                    if change["anterior_json"]
                    else None
                )
                proposed = (
                    json.loads(change["posterior_json"])
                    if change["posterior_json"]
                    else None
                )
                if previous and str(previous["worker_id"]) in allowed:
                    worker_id = str(previous["worker_id"])
                    inserted += _insert_notice(
                        connection,
                        publication_id=publication_id,
                        plan_version=int(publication["versio"]),
                        change_id=int(change["id"]),
                        worker_id=worker_id,
                        action="retirat",
                        notification_type="ALTERACIO_SERVEI",
                        variables={
                            "data": _display_date(str(change["data"])),
                            "detall": (
                                "Aquest servei ja no consta assignat a la "
                                f"versió V{publication['versio']} del pla."
                            ),
                            "serviceId": str(change["servei"]),
                        },
                    )
                if proposed and str(proposed["worker_id"]) in allowed:
                    assignment = connection.execute(
                        """
                        SELECT data, torn, zona, hora_inici, hora_fi
                        FROM assig_grup_T WHERE id = ?
                        """,
                        (change["assignacio_nova_id"],),
                    ).fetchone()
                    if assignment is None:
                        raise XivatoError(
                            "No s'ha trobat l'assignació publicada d'un avís"
                        )
                    worker_id = str(proposed["worker_id"])
                    turn = str(assignment["torn"] or change["servei"])
                    inserted += _insert_notice(
                        connection,
                        publication_id=publication_id,
                        plan_version=int(publication["versio"]),
                        change_id=int(change["id"]),
                        worker_id=worker_id,
                        action="assignat",
                        notification_type="SERVEI_ASSIGNAT_DIA",
                        variables={
                            "data": _display_date(str(assignment["data"])),
                            "zona": str(assignment["zona"] or "Sense zona indicada"),
                            "torn": turn,
                            "serviceId": str(change["servei"]),
                        },
                    )
        return {"creats": inserted, "canvis_revisats": len(changes)}
    finally:
        connection.close()


def list_xivato_notifications(
    database_path: str | Path,
    publication_id: int,
) -> list[dict]:
    migrate_planning_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT n.*, COALESCE(t.treballador, n.treballador_id) AS treballador
            FROM notificacions_xivato n
            LEFT JOIN treballadors t
              ON CAST(t.id AS TEXT) = CAST(n.treballador_id AS TEXT)
            WHERE n.publicacio_id = ?
            ORDER BY n.id
            """,
            (publication_id,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        variables = json.loads(item.pop("variables_json"))
        item["variables"] = variables
        item["data"] = variables.get("data", "")
        result.append(item)
    return result


def _payload(notice: dict, mode: str) -> dict:
    return {
        "treballadorId": notice["treballador_id"],
        "eventId": notice["event_id"],
        "mode": mode,
        "tipus": notice["tipus"],
        "variables": notice["variables"],
    }


def _record_result(
    database_path: str | Path,
    notice_id: int,
    *,
    state: str,
    response: dict | None = None,
    error: str | None = None,
    sent: bool = False,
) -> None:
    with sqlite3.connect(database_path) as connection, connection:
        connection.execute(
            """
            UPDATE notificacions_xivato
            SET estat = ?, resposta_json = ?, error = ?,
                intents = intents + ?,
                updated_at = CURRENT_TIMESTAMP,
                sent_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE sent_at END
            WHERE id = ?
            """,
            (
                state,
                json.dumps(response, ensure_ascii=False, sort_keys=True)
                if response is not None
                else None,
                error,
                int(sent),
                int(sent),
                notice_id,
            ),
        )


def preview_xivato_notifications(
    database_path: str | Path,
    publication_id: int,
    settings: XivatoSettings,
    *,
    sender: Sender = post_xivato,
) -> dict:
    notices = [
        item
        for item in list_xivato_notifications(database_path, publication_id)
        if item["estat"] in PREVIEWABLE_STATES
    ]
    mapped = 0
    unmapped = 0
    errors = 0
    for notice in notices:
        try:
            response = sender(_payload(notice, "previsualitzar"), settings)
            is_mapped = bool(response.get("mapejat"))
            state = "previsualitzat" if is_mapped else "sense_mapping"
            mapped += int(is_mapped)
            unmapped += int(not is_mapped)
            _record_result(
                database_path,
                int(notice["id"]),
                state=state,
                response=response,
            )
        except XivatoError as error:
            errors += 1
            _record_result(
                database_path,
                int(notice["id"]),
                state="error",
                error=str(error),
            )
    return {"mapejats": mapped, "sense_mapping": unmapped, "errors": errors}


def send_xivato_notifications(
    database_path: str | Path,
    publication_id: int,
    settings: XivatoSettings,
    *,
    sender: Sender = post_xivato,
) -> dict:
    notices = [
        item
        for item in list_xivato_notifications(database_path, publication_id)
        if item["estat"] == "previsualitzat"
    ]
    sent = 0
    duplicates = 0
    errors = 0
    for notice in notices:
        try:
            response = sender(_payload(notice, "enviar"), settings)
            accounts = int(response.get("comptes", 0))
            duplicate_accounts = int(response.get("duplicats", 0))
            persistent_errors = int(response.get("errorsPersistits", 0))
            is_duplicate = accounts > 0 and duplicate_accounts == accounts
            state = (
                "error"
                if persistent_errors
                else "duplicat"
                if is_duplicate
                else "enviat"
            )
            errors += int(bool(persistent_errors))
            duplicates += int(is_duplicate and not persistent_errors)
            sent += int(not is_duplicate and not persistent_errors)
            _record_result(
                database_path,
                int(notice["id"]),
                state=state,
                response=response,
                sent=True,
            )
        except XivatoError as error:
            errors += 1
            _record_result(
                database_path,
                int(notice["id"]),
                state="error",
                error=str(error),
                sent=True,
            )
    return {"enviats": sent, "duplicats": duplicates, "errors": errors}
