"""Tancament reversible de les hores efectivament realitzades."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path


class RealizedHoursError(ValueError):
    """Indica que el període no es pot tancar de manera segura."""


def _iso_day(value: date | str) -> str:
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as error:
        raise RealizedHoursError(f"Data invàlida: {value}") from error


def _readonly_connection(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path).resolve()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _duration_minutes(row: sqlite3.Row) -> int:
    duration = round(float(row["durada_hores"] or 0) * 60)
    if duration > 0:
        return duration
    try:
        start = datetime.strptime(str(row["hora_inici"]), "%H:%M").time()
        end = datetime.strptime(str(row["hora_fi"]), "%H:%M").time()
    except (TypeError, ValueError) as error:
        raise RealizedHoursError(
            f"L'assignació {row['id']} no té una durada vàlida."
        ) from error
    start_at = datetime.combine(date.min, start)
    end_at = datetime.combine(date.min, end)
    if end_at <= start_at:
        end_at += timedelta(days=1)
    return int((end_at - start_at).total_seconds() // 60)


def _fingerprint(items: list[dict[str, object]]) -> str:
    payload = json.dumps(
        items,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _preview_with_connection(
    connection: sqlite3.Connection,
    start_day: str,
    end_day: str,
) -> dict[str, object]:
    rows = list(
        connection.execute(
            """
            SELECT a.*, h.id AS hora_realitzada_id
            FROM assig_grup_T AS a
            LEFT JOIN hores_realitzades AS h
              ON h.data = a.data
             AND h.servei = a.torn
             AND h.estat = 'confirmada'
            WHERE a.estat_planificacio IN ('publicada', 'bloquejada')
              AND a.data BETWEEN ? AND ?
            ORDER BY a.data, a.torn, a.id
            """,
            (start_day, end_day),
        )
    )
    seen: set[tuple[str, str]] = set()
    assignments: list[dict[str, object]] = []
    for row in rows:
        need = (str(row["data"]), str(row["torn"]))
        if need in seen:
            raise RealizedHoursError(
                "El pla publicat conté més d'una assignació activa per a "
                f"{need[0]} / {need[1]}. Cal corregir-lo abans de tancar."
            )
        seen.add(need)
        assignments.append(
            {
                "assignacio_id": int(row["id"]),
                "data": need[0],
                "servei": need[1],
                "treballador_id": str(row["treballador_id"]),
                "treballador": str(row["treballador_nom"] or row["treballador_id"]),
                "hora_inici": str(row["hora_inici"] or ""),
                "hora_fi": str(row["hora_fi"] or ""),
                "durada_minuts": _duration_minutes(row),
                "es_canvi_zona": int(bool(row["es_canvi_zona"])),
                "es_canvi_torn": int(bool(row["es_canvi_torn"])),
                "estat_planificacio": str(row["estat_planificacio"]),
                "ja_confirmada": row["hora_realitzada_id"] is not None,
            }
        )
    pending = [item for item in assignments if not item["ja_confirmada"]]
    fingerprint_items = [
        {
            "assignacio_id": item["assignacio_id"],
            "data": item["data"],
            "servei": item["servei"],
            "treballador_id": item["treballador_id"],
            "hora_inici": item["hora_inici"],
            "hora_fi": item["hora_fi"],
            "durada_minuts": item["durada_minuts"],
            "es_canvi_zona": item["es_canvi_zona"],
            "es_canvi_torn": item["es_canvi_torn"],
            "estat_planificacio": item["estat_planificacio"],
            "ja_confirmada": item["ja_confirmada"],
        }
        for item in assignments
    ]
    return {
        "data_inici": start_day,
        "data_fi": end_day,
        "assignacions": assignments,
        "publicades": len(assignments),
        "pendents": len(pending),
        "ja_confirmades": len(assignments) - len(pending),
        "minuts_pendents": sum(int(item["durada_minuts"]) for item in pending),
        "empremta": _fingerprint(fingerprint_items),
    }


def preview_period_closure(
    database_path: str | Path,
    start_date: date | str,
    end_date: date | str,
) -> dict[str, object]:
    """Mostra què es confirmarà, sense escriure res a la base."""
    start_day = _iso_day(start_date)
    end_day = _iso_day(end_date)
    if start_day > end_day:
        raise RealizedHoursError("La data inicial ha de ser anterior a la final.")
    with closing(_readonly_connection(database_path)) as connection:
        return _preview_with_connection(connection, start_day, end_day)


def close_period(
    database_path: str | Path,
    start_date: date | str,
    end_date: date | str,
    *,
    expected_fingerprint: str,
) -> dict[str, object]:
    """Confirma en una sola transacció totes les assignacions pendents."""
    start_day = _iso_day(start_date)
    end_day = _iso_day(end_date)
    if start_day > end_day:
        raise RealizedHoursError("La data inicial ha de ser anterior a la final.")

    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            preview = _preview_with_connection(connection, start_day, end_day)
            if preview["empremta"] != expected_fingerprint:
                raise RealizedHoursError(
                    "El pla publicat ha canviat des de la previsualització. "
                    "Torna a revisar el període abans de confirmar-lo."
                )
            pending = [
                item
                for item in preview["assignacions"]
                if not item["ja_confirmada"]
            ]
            if not pending:
                connection.rollback()
                return {"tancament_id": None, **preview}

            total_minutes = sum(int(item["durada_minuts"]) for item in pending)
            cursor = connection.execute(
                """
                INSERT INTO tancaments_hores_realitzades
                (data_inici, data_fi, assignacions_confirmades, minuts_confirmats)
                VALUES (?, ?, ?, ?)
                """,
                (start_day, end_day, len(pending), total_minutes),
            )
            closure_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO hores_realitzades
                (tancament_id, assignacio_id, treballador_id, data, servei,
                 hora_inici, hora_fi, durada_minuts, es_canvi_zona,
                 es_canvi_torn)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        closure_id,
                        item["assignacio_id"],
                        item["treballador_id"],
                        item["data"],
                        item["servei"],
                        item["hora_inici"],
                        item["hora_fi"],
                        item["durada_minuts"],
                        item["es_canvi_zona"],
                        item["es_canvi_torn"],
                    )
                    for item in pending
                ),
            )
            connection.commit()
            return {
                "tancament_id": closure_id,
                **preview,
                "confirmades_ara": len(pending),
                "minuts_confirmats_ara": total_minutes,
            }
        except Exception:
            connection.rollback()
            raise


def reverse_closure(
    database_path: str | Path,
    closure_id: int,
    *,
    reason: str,
) -> dict[str, int]:
    """Anul·la un tancament sense esborrar-ne l'auditoria."""
    clean_reason = reason.strip()
    if not clean_reason:
        raise RealizedHoursError("Indica el motiu de la reversió.")
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            closure = connection.execute(
                "SELECT estat FROM tancaments_hores_realitzades WHERE id = ?",
                (closure_id,),
            ).fetchone()
            if closure is None:
                raise RealizedHoursError("El tancament indicat no existeix.")
            if closure["estat"] != "tancat":
                raise RealizedHoursError("Aquest tancament ja està revertit.")
            cursor = connection.execute(
                """
                UPDATE hores_realitzades
                SET estat = 'anul_lada', anul_lada_at = CURRENT_TIMESTAMP
                WHERE tancament_id = ? AND estat = 'confirmada'
                """,
                (closure_id,),
            )
            connection.execute(
                """
                UPDATE tancaments_hores_realitzades
                SET estat = 'revertit', reverted_at = CURRENT_TIMESTAMP,
                    motiu_reversio = ?
                WHERE id = ?
                """,
                (clean_reason, closure_id),
            )
            connection.commit()
            return {"tancament_id": closure_id, "hores_anul_lades": cursor.rowcount}
        except Exception:
            connection.rollback()
            raise


def coverage_date_limits(database_path: str | Path) -> tuple[date, date] | None:
    """Retorna els límits disponibles per proposar el selector de dates."""
    with closing(_readonly_connection(database_path)) as connection:
        row = connection.execute(
            "SELECT MIN(data), MAX(data) FROM cobertura WHERE data IS NOT NULL"
        ).fetchone()
    if row is None or row[0] is None or row[1] is None:
        return None
    return date.fromisoformat(str(row[0])), date.fromisoformat(str(row[1]))


def list_closures(
    database_path: str | Path,
    *,
    limit: int = 50,
) -> list[dict[str, object]]:
    with closing(_readonly_connection(database_path)) as connection:
        rows = connection.execute(
            """
            SELECT id, data_inici, data_fi, estat, assignacions_confirmades,
                   minuts_confirmats, created_at, reverted_at, motiu_reversio
            FROM tancaments_hores_realitzades
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def realized_hours_by_worker(
    database_path: str | Path,
    *,
    year: int | None = None,
) -> list[dict[str, object]]:
    """Calcula el total derivat; no el duplica a la fitxa del treballador."""
    year_filter = "AND substr(h.data, 1, 4) = ?" if year is not None else ""
    parameters: tuple[object, ...] = (str(year),) if year is not None else ()
    with closing(_readonly_connection(database_path)) as connection:
        rows = connection.execute(
            f"""
            SELECT CAST(t.id AS TEXT) AS treballador_id,
                   t.treballador,
                   COALESCE(SUM(h.durada_minuts), 0) AS minuts_realitzats,
                   COALESCE(SUM(h.es_canvi_torn), 0) AS canvis_torn,
                   COALESCE(SUM(h.es_canvi_zona), 0) AS canvis_zona,
                   COUNT(h.id) AS serveis_confirmats,
                   CASE WHEN COUNT(h.id) = 0 THEN 0.0 ELSE ROUND(
                       100.0 * COALESCE(SUM(h.es_canvi_torn), 0)
                       / COUNT(h.id), 1
                   ) END AS percentatge_canvis_torn,
                   CASE WHEN COUNT(h.id) = 0 THEN 0.0 ELSE ROUND(
                       100.0 * COALESCE(SUM(h.es_canvi_zona), 0)
                       / COUNT(h.id), 1
                   ) END AS percentatge_canvis_zona
            FROM treballadors AS t
            LEFT JOIN hores_realitzades AS h
              ON h.treballador_id = CAST(t.id AS TEXT)
             AND h.estat = 'confirmada'
             {year_filter}
            WHERE t.grup = 'T'
            GROUP BY t.id, t.treballador
            ORDER BY minuts_realitzats DESC, t.treballador
            """,
            parameters,
        ).fetchall()
    return [
        {
            **dict(row),
            "hores_realitzades": round(int(row["minuts_realitzats"]) / 60, 2),
        }
        for row in rows
    ]


def list_realized_hours(
    database_path: str | Path,
    *,
    year: int | None = None,
    include_reversed: bool = False,
) -> list[dict[str, object]]:
    clauses = [] if include_reversed else ["h.estat = 'confirmada'"]
    parameters: list[object] = []
    if year is not None:
        clauses.append("substr(h.data, 1, 4) = ?")
        parameters.append(str(year))
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    with closing(_readonly_connection(database_path)) as connection:
        rows = connection.execute(
            f"""
            SELECT h.id, h.data, h.servei, h.treballador_id,
                   COALESCE(t.treballador, h.treballador_id) AS treballador,
                   h.hora_inici, h.hora_fi, h.durada_minuts, h.estat,
                   h.tancament_id, h.confirmada_at, h.anul_lada_at
            FROM hores_realitzades AS h
            LEFT JOIN treballadors AS t
              ON CAST(t.id AS TEXT) = h.treballador_id
            {where}
            ORDER BY h.data DESC, h.servei, h.id DESC
            """,
            tuple(parameters),
        ).fetchall()
    return [dict(row) for row in rows]
