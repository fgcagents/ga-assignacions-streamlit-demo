"""Cicle controlat d'incidències i propostes de replanificació.

No executa l'algorisme genètic ni modifica el pla publicat durant la simulació.
Només l'aprovació explícita aplica la incidència i anul·la les assignacions afectades.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from servei_descansos import _aplica_substitucio_dia, _conflictes_substitut


TIPUS_INCIDENCIA = ("baixa", "vacances", "substitucio", "alta_anticipada", "prorroga_baixa")
TIPUS_INCIDENCIA_CP_SAT = frozenset(TIPUS_INCIDENCIA)


@contextmanager
def _connexio(db_path: str | Path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _files(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def inicialitza_planificacio(db_path: str | Path) -> None:
    """Crea les taules de control sense tocar cap assignació existent."""
    with _connexio(db_path) as conn:
        columnes = {fila["name"] for fila in conn.execute("PRAGMA table_info(assig_grup_T)")}
        if "estat_planificacio" not in columnes:
            conn.execute(
                "ALTER TABLE assig_grup_T ADD COLUMN estat_planificacio TEXT NOT NULL DEFAULT 'publicada'"
            )
        definicio_incidencies = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'incidencies_personal'"
        ).fetchone()
        if definicio_incidencies and "alta_anticipada" not in (definicio_incidencies["sql"] or ""):
            conn.executescript(
                """
                ALTER TABLE incidencies_personal RENAME TO incidencies_personal_v1;
                CREATE TABLE incidencies_personal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    treballador_id INTEGER NOT NULL,
                    tipus TEXT NOT NULL CHECK (tipus IN ('baixa', 'vacances', 'substitucio', 'alta_anticipada', 'prorroga_baixa')),
                    estat TEXT NOT NULL DEFAULT 'registrada'
                        CHECK (estat IN ('registrada', 'en_proposta', 'aprovada', 'anul_lada')),
                    data_comunicacio TEXT NOT NULL, data_inici TEXT NOT NULL, data_fi TEXT NOT NULL,
                    treballador_substitut_id INTEGER, motiu TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO incidencies_personal
                (id, treballador_id, tipus, estat, data_comunicacio, data_inici, data_fi, motiu, created_at)
                SELECT id, treballador_id, tipus, estat, data_comunicacio, data_inici, data_fi, motiu, created_at
                FROM incidencies_personal_v1;
                DROP TABLE incidencies_personal_v1;
                """
            )
        columnes_incidencies = {fila["name"] for fila in conn.execute("PRAGMA table_info(incidencies_personal)")}
        if columnes_incidencies and "treballador_substitut_id" not in columnes_incidencies:
            conn.execute("ALTER TABLE incidencies_personal ADD COLUMN treballador_substitut_id INTEGER")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS incidencies_personal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                treballador_id INTEGER NOT NULL,
                tipus TEXT NOT NULL CHECK (tipus IN ('baixa', 'vacances', 'substitucio')),
                estat TEXT NOT NULL DEFAULT 'registrada'
                    CHECK (estat IN ('registrada', 'en_proposta', 'aprovada', 'anul_lada')),
                data_comunicacio TEXT NOT NULL,
                data_inici TEXT NOT NULL,
                data_fi TEXT NOT NULL,
                treballador_substitut_id INTEGER,
                motiu TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (treballador_id) REFERENCES treballadors(id)
            );
            CREATE TABLE IF NOT EXISTS propostes_replanificacio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incidencia_id INTEGER NOT NULL,
                estat TEXT NOT NULL DEFAULT 'esborrany'
                    CHECK (estat IN ('esborrany', 'aprovada', 'anul_lada')),
                data_inici TEXT NOT NULL,
                data_fi TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                aprovada_at TEXT,
                FOREIGN KEY (incidencia_id) REFERENCES incidencies_personal(id)
            );
            CREATE TABLE IF NOT EXISTS proposta_canvis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposta_id INTEGER NOT NULL,
                tipus TEXT NOT NULL,
                assignacio_id INTEGER,
                data TEXT NOT NULL,
                torn TEXT,
                treballador_id INTEGER,
                descripcio TEXT NOT NULL,
                FOREIGN KEY (proposta_id) REFERENCES propostes_replanificacio(id)
            );
            CREATE TABLE IF NOT EXISTS auditoria_planificacio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entitat TEXT NOT NULL,
                entitat_id INTEGER NOT NULL,
                accio TEXT NOT NULL,
                detall TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        columnes_propostes = {
            fila["name"]
            for fila in conn.execute(
                "PRAGMA table_info(propostes_replanificacio)"
            )
        }
        for nom, definicio in (
            ("motor", "TEXT NOT NULL DEFAULT 'detector'"),
            ("snapshot_hash", "TEXT"),
            ("solver_status", "TEXT"),
            ("necessitats_cobertes", "INTEGER"),
            ("necessitats_totals", "INTEGER"),
            ("errors_validacio", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if nom not in columnes_propostes:
                conn.execute(
                    f"ALTER TABLE propostes_replanificacio "
                    f"ADD COLUMN {nom} {definicio}"
                )
        columnes_canvis = {
            fila["name"]
            for fila in conn.execute("PRAGMA table_info(proposta_canvis)")
        }
        if "assignacio_nova_id" not in columnes_canvis:
            conn.execute(
                "ALTER TABLE proposta_canvis "
                "ADD COLUMN assignacio_nova_id INTEGER"
            )
        if "necessitat_id" not in columnes_canvis:
            conn.execute(
                "ALTER TABLE proposta_canvis ADD COLUMN necessitat_id TEXT"
            )
        for nom, definicio in (
            ("hora_inici", "TEXT"),
            ("hora_fi", "TEXT"),
            ("durada_hores", "REAL"),
            ("zona", "TEXT"),
        ):
            if nom not in columnes_canvis:
                conn.execute(
                    f"ALTER TABLE proposta_canvis ADD COLUMN {nom} {definicio}"
                )


def _audita(conn: sqlite3.Connection, entitat: str, entitat_id: int, accio: str, detall: str) -> None:
    conn.execute(
        "INSERT INTO auditoria_planificacio (entitat, entitat_id, accio, detall) VALUES (?, ?, ?, ?)",
        (entitat, entitat_id, accio, detall),
    )


def _valors_separats(valor: str | None) -> set[str]:
    """Normalitza camps de formació desats com a text a SQLite."""
    if not valor:
        return set()
    return {
        part.strip().upper()
        for part in valor.replace("+", ",").replace(";", ",").split(",")
        if part.strip()
    }


def _interval_assignacio(fila: sqlite3.Row) -> tuple[datetime, datetime] | None:
    try:
        inici = datetime.fromisoformat(f"{fila['data']}T{fila['hora_inici']}")
        fi = datetime.fromisoformat(f"{fila['data']}T{fila['hora_fi']}")
    except (TypeError, ValueError):
        return None
    if fi <= inici:
        fi += timedelta(days=1)
    return inici, fi


def _compleix_descans_12h(
    conn: sqlite3.Connection,
    treballador_id: int | str,
    assignacio: sqlite3.Row,
) -> bool:
    """Descarta solapaments i descansos inferiors a 12 h amb el pla vigent."""
    interval_nou = _interval_assignacio(assignacio)
    if interval_nou is None:
        return False
    inici_nou, fi_nou = interval_nou
    inici_finestra = (date.fromisoformat(assignacio["data"]) - timedelta(days=2)).isoformat()
    fi_finestra = (date.fromisoformat(assignacio["data"]) + timedelta(days=2)).isoformat()
    existents = conn.execute(
        """
        SELECT data, hora_inici, hora_fi
        FROM assig_grup_T
        WHERE CAST(treballador_id AS TEXT) = CAST(? AS TEXT)
          AND data BETWEEN ? AND ?
          AND estat_planificacio IN ('publicada', 'bloquejada')
        """,
        (treballador_id, inici_finestra, fi_finestra),
    ).fetchall()
    for existent in existents:
        interval_existent = _interval_assignacio(existent)
        if interval_existent is None:
            continue
        inici_existent, fi_existent = interval_existent
        if fi_existent <= inici_nou:
            descans = (inici_nou - fi_existent).total_seconds() / 3600
        elif fi_nou <= inici_existent:
            descans = (inici_existent - fi_nou).total_seconds() / 3600
        else:
            return False
        if descans < 12:
            return False
    return True


def _candidats_cobertura(
    conn: sqlite3.Connection,
    assignacio: sqlite3.Row,
    treballador_afectat_id: int | str,
    treballador_substitut_id: int | str | None = None,
) -> list[dict[str, Any]]:
    """Retorna personal T disponible o amb descans base per a l'assignació."""
    formacions_requerides = _valors_separats(assignacio["formacio"])
    candidats: list[dict[str, Any]] = []
    treballadors = conn.execute(
        """
        SELECT id, treballador, plaza, rotacio, zona, habilitacions
        FROM treballadors
        WHERE grup = 'T' AND CAST(id AS TEXT) <> CAST(? AS TEXT)
        ORDER BY treballador
        """,
        (treballador_afectat_id,),
    ).fetchall()
    for treballador in treballadors:
        if formacions_requerides and not (
            formacions_requerides
            & _valors_separats(treballador["habilitacions"])
        ):
            continue
        if conn.execute(
            """
            SELECT 1 FROM assig_grup_T
            WHERE CAST(treballador_id AS TEXT) = CAST(? AS TEXT)
              AND data = ?
              AND estat_planificacio IN ('publicada', 'bloquejada')
            LIMIT 1
            """,
            (treballador["id"], assignacio["data"]),
        ).fetchone():
            continue
        if conn.execute(
            """
            SELECT 1 FROM descansos_dies
            WHERE CAST(treballador_substitut_id AS TEXT) = CAST(? AS TEXT)
              AND data = ?
            LIMIT 1
            """,
            (treballador["id"], assignacio["data"]),
        ).fetchone():
            continue
        descansos = conn.execute(
            """
            SELECT origen FROM descansos_dies
            WHERE CAST(treballador_id AS TEXT) = CAST(? AS TEXT) AND data = ?
            """,
            (treballador["id"], assignacio["data"]),
        ).fetchall()
        origens = {str(fila["origen"] or "").lower() for fila in descansos}
        if origens and origens != {"base"}:
            continue
        if not _compleix_descans_12h(conn, treballador["id"], assignacio):
            continue
        estat = "descans_base" if origens else "disponible"
        candidats.append({
            "id": treballador["id"],
            "treballador": treballador["treballador"],
            "plaza": treballador["plaza"],
            "estat_disponibilitat": estat,
            "substitut_indicat": (
                treballador_substitut_id is not None
                and str(treballador["id"]) == str(treballador_substitut_id)
            ),
        })
    return sorted(
        candidats,
        key=lambda candidat: (
            not candidat["substitut_indicat"],
            candidat["estat_disponibilitat"] != "disponible",
            candidat["treballador"],
        ),
    )


def registrar_incidencia(
    db_path: str | Path, treballador_id: int, tipus: str, data_comunicacio: date,
    data_inici: date, data_fi: date, motiu: str = "", treballador_substitut_id: int | None = None,
) -> int:
    if tipus not in TIPUS_INCIDENCIA:
        raise ValueError("Tipus d'incidència no admès")
    if data_fi < data_inici:
        raise ValueError("La data final no pot ser anterior a la data inicial")
    inicialitza_planificacio(db_path)
    with _connexio(db_path) as conn:
        if not conn.execute("SELECT 1 FROM treballadors WHERE id = ?", (treballador_id,)).fetchone():
            raise ValueError("El treballador seleccionat no existeix")
        if tipus == "substitucio":
            if treballador_substitut_id is None or treballador_substitut_id == treballador_id:
                raise ValueError("Selecciona un substitut diferent del treballador afectat")
            if not conn.execute("SELECT 1 FROM treballadors WHERE id = ?", (treballador_substitut_id,)).fetchone():
                raise ValueError("El treballador substitut no existeix")
        cursor = conn.execute(
            """INSERT INTO incidencies_personal
               (treballador_id, tipus, data_comunicacio, data_inici, data_fi, motiu, treballador_substitut_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (treballador_id, tipus, data_comunicacio.isoformat(), data_inici.isoformat(), data_fi.isoformat(), motiu or None, treballador_substitut_id),
        )
        incidencia_id = cursor.lastrowid
        _audita(conn, "incidencia", incidencia_id, "registrada", f"{tipus}: {data_inici} a {data_fi}")
    return int(incidencia_id)


def llista_incidencies(db_path: str | Path) -> list[dict[str, Any]]:
    inicialitza_planificacio(db_path)
    with _connexio(db_path) as conn:
        rows = conn.execute(
            """SELECT i.*, t.treballador, t.plaza, s.treballador AS substitut
               FROM incidencies_personal i JOIN treballadors t ON t.id = i.treballador_id
               LEFT JOIN treballadors s ON s.id = i.treballador_substitut_id
               ORDER BY CASE i.estat WHEN 'registrada' THEN 0 WHEN 'en_proposta' THEN 1 ELSE 2 END,
                        i.data_inici DESC, i.id DESC"""
        ).fetchall()
    return _files(rows)


def generar_proposta(db_path: str | Path, incidencia_id: int) -> dict[str, Any]:
    """Genera una simulació CP-SAT quan el tipus d'incidència ja és compatible."""
    inicialitza_planificacio(db_path)
    with _connexio(db_path) as conn:
        incidencia = conn.execute(
            "SELECT tipus FROM incidencies_personal WHERE id = ?",
            (incidencia_id,),
        ).fetchone()
    if not incidencia:
        raise ValueError("No s'ha trobat la incidència")
    if incidencia["tipus"] not in TIPUS_INCIDENCIA_CP_SAT:
        return _generar_proposta_detector(db_path, incidencia_id)

    try:
        from servei_replanificacio_cp_sat import generate_incident_draft
    except ModuleNotFoundError as exc:
        if exc.name == "ortools":
            raise ValueError(
                "Cal instal·lar OR-Tools per generar la proposta CP-SAT"
            ) from exc
        raise

    draft = generate_incident_draft(db_path, incidencia_id)
    result = draft.result
    with _connexio(db_path) as conn:
        incidencia_actual = conn.execute(
            "SELECT estat FROM incidencies_personal WHERE id = ?",
            (incidencia_id,),
        ).fetchone()
        if not incidencia_actual or incidencia_actual["estat"] in (
            "aprovada",
            "anul_lada",
        ):
            raise ValueError("La incidència ja no està disponible per simular")

        existent = conn.execute(
            """
            SELECT id FROM propostes_replanificacio
            WHERE incidencia_id = ? AND estat = 'esborrany'
            """,
            (incidencia_id,),
        ).fetchone()
        if existent:
            proposta_id = int(existent["id"])
            conn.execute(
                "DELETE FROM proposta_canvis WHERE proposta_id = ?",
                (proposta_id,),
            )
        else:
            proposta_id = int(
                conn.execute(
                    """
                    INSERT INTO propostes_replanificacio
                    (incidencia_id, data_inici, data_fi, motor)
                    VALUES (?, ?, ?, 'cp_sat')
                    """,
                    (
                        incidencia_id,
                        draft.context.start_date.isoformat(),
                        draft.context.end_date.isoformat(),
                    ),
                ).lastrowid
            )

        conn.execute(
            """
            UPDATE propostes_replanificacio
            SET motor = 'cp_sat', data_inici = ?, data_fi = ?,
                snapshot_hash = ?, solver_status = ?,
                necessitats_cobertes = ?, necessitats_totals = ?,
                errors_validacio = ?
            WHERE id = ?
            """,
            (
                draft.context.start_date.isoformat(),
                draft.context.end_date.isoformat(),
                draft.context.snapshot_hash,
                result.status if result else "NO_EXECUTAT",
                result.covered_needs if result else 0,
                result.total_needs if result else 0,
                len(result.validation_errors) if result else 0,
                proposta_id,
            ),
        )
        for canvi in draft.changes:
            conn.execute(
                """
                INSERT INTO proposta_canvis
                (proposta_id, tipus, assignacio_id, necessitat_id, data, torn,
                 treballador_id, descripcio, hora_inici, hora_fi,
                 durada_hores, zona)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposta_id,
                    canvi["tipus"],
                    canvi["assignacio_id"],
                    canvi["necessitat_id"],
                    canvi["data"],
                    canvi["torn"],
                    canvi["treballador_id"],
                    canvi["descripcio"],
                    canvi.get("hora_inici"),
                    canvi.get("hora_fi"),
                    canvi.get("durada_hores"),
                    canvi.get("zona"),
                ),
            )
        conn.execute(
            "UPDATE incidencies_personal SET estat = 'en_proposta' WHERE id = ?",
            (incidencia_id,),
        )
        _audita(
            conn,
            "proposta",
            proposta_id,
            "simulada_cp_sat",
            (
                f"snapshot={draft.context.snapshot_hash}; "
                f"cobertura="
                f"{result.covered_needs if result else 0}/"
                f"{result.total_needs if result else 0}; "
                f"canvis={len(draft.changes)}"
            ),
        )
    return obtenir_proposta(db_path, proposta_id)


def _generar_proposta_detector(
    db_path: str | Path,
    incidencia_id: int,
) -> dict[str, Any]:
    """Detecta canvis necessaris sense modificar ni descansos ni assignacions."""
    inicialitza_planificacio(db_path)
    with _connexio(db_path) as conn:
        incidencia = conn.execute(
            """SELECT i.*, t.treballador, t.plaza FROM incidencies_personal i
               JOIN treballadors t ON t.id = i.treballador_id WHERE i.id = ?""",
            (incidencia_id,),
        ).fetchone()
        if not incidencia:
            raise ValueError("No s'ha trobat la incidència")
        if incidencia["estat"] in ("aprovada", "anul_lada"):
            raise ValueError("Aquesta incidència ja està tancada")

        existent = conn.execute(
            "SELECT id FROM propostes_replanificacio WHERE incidencia_id = ? AND estat = 'esborrany'",
            (incidencia_id,),
        ).fetchone()
        if existent:
            proposta_id = existent["id"]
            conn.execute("DELETE FROM proposta_canvis WHERE proposta_id = ?", (proposta_id,))
        else:
            proposta_id = conn.execute(
                "INSERT INTO propostes_replanificacio (incidencia_id, data_inici, data_fi) VALUES (?, ?, ?)",
                (incidencia_id, incidencia["data_inici"], incidencia["data_fi"]),
            ).lastrowid

        treballador_impacte = -1 if incidencia["tipus"] == "alta_anticipada" else incidencia["treballador_id"]
        afectades = conn.execute(
            """SELECT id, data, torn, treballador_id, hora_inici, hora_fi,
                      formacio, linia, zona
               FROM assig_grup_T
               WHERE treballador_id = ? AND data BETWEEN ? AND ?
                 AND estat_planificacio IN ('publicada', 'bloquejada')
               ORDER BY data, torn""",
            (treballador_impacte, incidencia["data_inici"], incidencia["data_fi"]),
        ).fetchall()
        for fila in afectades:
            conn.execute(
                """INSERT INTO proposta_canvis
                   (proposta_id, tipus, assignacio_id, data, torn, treballador_id, descripcio)
                   VALUES (?, 'assignacio_a_reemplaçar', ?, ?, ?, ?, ?)""",
                (proposta_id, fila["id"], fila["data"], fila["torn"], fila["treballador_id"],
                 "Assignació afectada per la incidència; cal buscar cobertura."),
            )
            candidats = _candidats_cobertura(
                conn,
                fila,
                incidencia["treballador_id"],
                incidencia["treballador_substitut_id"],
            )
            for candidat in candidats:
                estat = (
                    "disponible"
                    if candidat["estat_disponibilitat"] == "disponible"
                    else "amb descans base"
                )
                indicat = " · substitut indicat" if candidat["substitut_indicat"] else ""
                conn.execute(
                    """INSERT INTO proposta_canvis
                       (proposta_id, tipus, assignacio_id, data, torn,
                        treballador_id, descripcio)
                       VALUES (?, 'candidat_cobertura', ?, ?, ?, ?, ?)""",
                    (
                        proposta_id,
                        fila["id"],
                        fila["data"],
                        fila["torn"],
                        candidat["id"],
                        (
                            f"{candidat['treballador']} · {candidat['plaza']} · "
                            f"{estat}{indicat}"
                        ),
                    ),
                )
            if not candidats:
                conn.execute(
                    """INSERT INTO proposta_canvis
                       (proposta_id, tipus, assignacio_id, data, torn, descripcio)
                       VALUES (?, 'servei_sense_cobertura', ?, ?, ?, ?)""",
                    (
                        proposta_id,
                        fila["id"],
                        fila["data"],
                        fila["torn"],
                        (
                            "Aquesta assignació queda sense cap candidat compatible "
                            "disponible o amb descans base."
                        ),
                    ),
                )

        conn.execute("UPDATE incidencies_personal SET estat = 'en_proposta' WHERE id = ?", (incidencia_id,))
        _audita(conn, "proposta", proposta_id, "simulada", "Proposta creada sense modificar el pla publicat")
    return obtenir_proposta(db_path, int(proposta_id))


def obtenir_proposta(db_path: str | Path, proposta_id: int) -> dict[str, Any]:
    inicialitza_planificacio(db_path)
    with _connexio(db_path) as conn:
        proposta = conn.execute("SELECT * FROM propostes_replanificacio WHERE id = ?", (proposta_id,)).fetchone()
        if not proposta:
            raise ValueError("No s'ha trobat la proposta")
        canvis = conn.execute(
            "SELECT * FROM proposta_canvis "
            "WHERE proposta_id = ? ORDER BY data, id",
            (proposta_id,),
        ).fetchall()
        noms_treballadors = {
            str(fila["id"]): fila["treballador"]
            for fila in conn.execute("SELECT id, treballador FROM treballadors")
        }
    resultat = dict(proposta)
    resultat["canvis"] = _files(canvis)
    for canvi in resultat["canvis"]:
        treballador_id = canvi.get("treballador_id")
        canvi["treballador_nom"] = (
            noms_treballadors.get(str(treballador_id), str(treballador_id))
            if treballador_id is not None
            else None
        )
    serveis_pendents = sum(1 for canvi in resultat["canvis"] if canvi["tipus"] == "servei_sense_cobertura")
    assignacions_afectades = sum(1 for canvi in resultat["canvis"] if canvi["tipus"] == "assignacio_a_reemplaçar")
    assignacions_proposades = sum(
        1
        for canvi in resultat["canvis"]
        if canvi["tipus"] == "assignacio_proposada"
    )
    if serveis_pendents:
        resultat["recomanacio"] = "Replanificació general recomanada"
        resultat["motiu_recomanacio"] = (
            f"Hi ha {serveis_pendents} assignació/ns afectada/es sense cap candidat "
            "compatible disponible o amb descans base."
        )
    elif assignacions_afectades or assignacions_proposades:
        resultat["recomanacio"] = "Replanificació parcial recomanada"
        resultat["motiu_recomanacio"] = (
            f"Hi ha {assignacions_afectades} assignació/ns afectada/es i "
            f"{assignacions_proposades} cobertura/es proposada/es, "
            "sense serveis descoberts."
        )
    else:
        resultat["recomanacio"] = "No cal replanificació"
        resultat["motiu_recomanacio"] = "No s'han detectat assignacions afectades ni serveis sense cobertura mínima."
    return resultat


def llista_propostes(db_path: str | Path) -> list[dict[str, Any]]:
    inicialitza_planificacio(db_path)
    with _connexio(db_path) as conn:
        rows = conn.execute(
            """SELECT p.*, i.tipus, t.treballador, t.plaza,
                      COUNT(CASE WHEN c.tipus = 'assignacio_a_reemplaçar' THEN 1 END)
                          AS total_assignacions_afectades,
                      COUNT(CASE WHEN c.tipus = 'candidat_cobertura' THEN 1 END)
                          AS total_opcions_cobertura,
                      COUNT(CASE WHEN c.tipus = 'assignacio_proposada' THEN 1 END)
                          AS total_assignacions_proposades,
                      COUNT(CASE WHEN c.tipus = 'servei_sense_cobertura' THEN 1 END)
                          AS total_sense_cobertura
               FROM propostes_replanificacio p
               JOIN incidencies_personal i ON i.id = p.incidencia_id
               JOIN treballadors t ON t.id = i.treballador_id
               LEFT JOIN proposta_canvis c ON c.proposta_id = p.id
               GROUP BY p.id ORDER BY p.created_at DESC, p.id DESC"""
        ).fetchall()
    return _files(rows)


def _files_pla_actiu(
    conn: sqlite3.Connection,
    data_inici: str,
    data_fi: str,
    *,
    buffer_days: int = 0,
) -> list[sqlite3.Row]:
    start = date.fromisoformat(data_inici) - timedelta(days=buffer_days)
    end = date.fromisoformat(data_fi) + timedelta(days=buffer_days)
    return conn.execute(
        """
        SELECT id, data, dia_setmana, torn, treballador_id, hora_inici, hora_fi,
               durada_hores, formacio, linia, zona, estat_planificacio
        FROM assig_grup_T
        WHERE data BETWEEN ? AND ?
          AND estat_planificacio IN ('publicada', 'bloquejada')
        ORDER BY data, torn, id
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()


def _valida_estructura_canvis_cp_sat(
    canvis: list[sqlite3.Row],
) -> tuple[
    set[int],
    dict[int, sqlite3.Row],
    set[int],
    dict[str, sqlite3.Row],
    set[str],
]:
    reemplaçades: set[int] = set()
    proposades: dict[int, sqlite3.Row] = {}
    pendents: set[int] = set()
    incorporades: dict[str, sqlite3.Row] = {}
    descobertes_noves: set[str] = set()
    for canvi in canvis:
        assignacio_id = canvi["assignacio_id"]
        if assignacio_id is None:
            necessitat_id = canvi["necessitat_id"]
            if not necessitat_id:
                raise ValueError(
                    "La proposta conté un canvi nou sense identificador "
                    "de necessitat"
                )
            necessitat_id = str(necessitat_id)
            if canvi["tipus"] == "assignacio_proposada":
                if necessitat_id in incorporades:
                    raise ValueError(
                        "La proposta conté una cobertura nova duplicada"
                    )
                if canvi["treballador_id"] is None:
                    raise ValueError("La cobertura nova no té treballador")
                incorporades[necessitat_id] = canvi
            elif canvi["tipus"] == "servei_sense_cobertura":
                if necessitat_id in descobertes_noves:
                    raise ValueError(
                        "La proposta conté un descobert nou duplicat"
                    )
                descobertes_noves.add(necessitat_id)
            else:
                raise ValueError(
                    "Un canvi sense origen només pot ser una cobertura "
                    "nova o un servei descobert"
                )
            continue
        assignacio_id = int(assignacio_id)
        if canvi["tipus"] == "assignacio_a_reemplaçar":
            if assignacio_id in reemplaçades:
                raise ValueError("La proposta conté una anul·lació duplicada")
            reemplaçades.add(assignacio_id)
        elif canvi["tipus"] == "assignacio_proposada":
            if assignacio_id in proposades:
                raise ValueError("La proposta conté una cobertura duplicada")
            if canvi["treballador_id"] is None:
                raise ValueError("La cobertura proposada no té treballador")
            proposades[assignacio_id] = canvi
        elif canvi["tipus"] == "servei_sense_cobertura":
            if assignacio_id in pendents:
                raise ValueError("La proposta conté un descobert duplicat")
            pendents.add(assignacio_id)
        else:
            raise ValueError(
                f"Tipus de canvi CP-SAT desconegut: {canvi['tipus']}"
            )

    if set(proposades) - reemplaçades or pendents - reemplaçades:
        raise ValueError(
            "La proposta conté cobertures sense anul·lació d'origen"
        )
    for assignacio_id in reemplaçades:
        alternatives = int(assignacio_id in proposades) + int(
            assignacio_id in pendents
        )
        if alternatives != 1:
            raise ValueError(
                "Cada assignació reemplaçada ha de tenir exactament "
                "una cobertura proposada o quedar descoberta"
            )
    if set(incorporades) & descobertes_noves:
        raise ValueError(
            "Una necessitat nova no pot quedar coberta i descoberta alhora"
        )
    return (
        reemplaçades,
        proposades,
        pendents,
        incorporades,
        descobertes_noves,
    )


def _es_canvi_torn(
    conn: sqlite3.Connection,
    worker_rotation: str | None,
    day: str,
    turn: str,
) -> int:
    coverage = conn.execute(
        """
        SELECT rotacio, torn FROM cobertura
        WHERE data = ? AND servei = ?
        LIMIT 1
        """,
        (day, turn),
    ).fetchone()
    if coverage is None:
        return 0
    required = _valors_separats(coverage["rotacio"] or coverage["torn"])
    worker_turns = _valors_separats(worker_rotation)
    return int(bool(required and worker_turns and required.isdisjoint(worker_turns)))


def _aprovar_proposta_cp_sat(
    conn: sqlite3.Connection,
    proposta: sqlite3.Row,
    proposta_id: int,
) -> dict[str, int]:
    from servei_replanificacio_cp_sat import (
        plan_snapshot_hash,
        prepare_incident_problem,
    )
    from cp_sat_pilot import Assignment, CpSatPlanner

    current_rows = _files_pla_actiu(
        conn,
        proposta["data_inici"],
        proposta["data_fi"],
        buffer_days=2,
    )
    current_hash = plan_snapshot_hash(current_rows)
    if not proposta["snapshot_hash"] or current_hash != proposta["snapshot_hash"]:
        raise ValueError(
            "El pla publicat ha canviat des que es va generar la proposta. "
            "Cal recalcular-la abans d'aprovar."
        )

    canvis = conn.execute(
        """
        SELECT * FROM proposta_canvis
        WHERE proposta_id = ?
        ORDER BY id
        """,
        (proposta_id,),
    ).fetchall()
    (
        reemplaçades,
        proposades,
        pendents,
        incorporades,
        descobertes_noves,
    ) = _valida_estructura_canvis_cp_sat(canvis)
    rows_by_id = {int(row["id"]): row for row in current_rows}
    if not reemplaçades.issubset(rows_by_id):
        raise ValueError(
            "Alguna assignació d'origen ja no forma part del pla vigent"
        )

    context = prepare_incident_problem(
        conn.execute("PRAGMA database_list").fetchone()[2],
        int(proposta["incidencia_id"]),
    )
    if context.snapshot_hash != current_hash:
        raise ValueError(
            "El pla ha canviat durant la revalidació. Cal recalcular la proposta."
        )
    source_by_need = {source.need_id: source for source in context.sources}
    final_assignments = []
    for reference in context.problem.reference_assignments:
        source = source_by_need[reference.need_id]
        if source.assignment_id not in reemplaçades:
            final_assignments.append(reference)
            continue
        proposed_change = proposades.get(source.assignment_id)
        if proposed_change is not None:
            final_assignments.append(
                replace(
                    reference,
                    worker_id=str(proposed_change["treballador_id"]),
                )
            )
    need_by_id = {need.id: need for need in context.problem.needs}
    unknown_new_needs = (
        set(incorporades) | descobertes_noves
    ) - set(need_by_id)
    if unknown_new_needs:
        raise ValueError(
            "La proposta conté necessitats que ja no existeixen: "
            + ", ".join(sorted(unknown_new_needs))
        )
    for need_id, proposed_change in incorporades.items():
        need = need_by_id[need_id]
        final_assignments.append(
            Assignment(
                worker_id=str(proposed_change["treballador_id"]),
                need_id=need.id,
                service_id=need.service_id,
                date=need.date,
                start=need.start,
                end=need.end,
                duration_minutes=need.duration_minutes,
            )
        )

    missing_affected = {
        source_by_need[need_id].assignment_id
        for need_id in context.problem.affected_need_ids
    } - reemplaçades
    if missing_affected:
        raise ValueError(
            "La proposta no reemplaça totes les assignacions afectades"
        )

    validation_errors = CpSatPlanner(context.problem).validate(final_assignments)
    if validation_errors:
        raise ValueError(
            "La proposta ja no compleix les restriccions dures: "
            + "; ".join(validation_errors[:5])
        )
    if proposta["necessitats_cobertes"] is not None and (
        len(final_assignments) != int(proposta["necessitats_cobertes"])
    ):
        raise ValueError(
            "La cobertura desada no coincideix amb els canvis de la proposta"
        )

    start = date.fromisoformat(proposta["data_inici"])
    end = date.fromisoformat(proposta["data_fi"])
    days = 0
    substitute_days_activated = 0
    if proposta["tipus"] == "alta_anticipada":
        cursor = conn.execute(
            """
            DELETE FROM descansos_dies
            WHERE CAST(treballador_id AS TEXT) = CAST(? AS TEXT)
              AND origen = 'baixa' AND data BETWEEN ? AND ?
            """,
            (
                proposta["treballador_id"],
                start.isoformat(),
                end.isoformat(),
            ),
        )
        days = cursor.rowcount
    elif proposta["tipus"] == "substitucio":
        substitute_id = proposta["treballador_substitut_id"]
        if substitute_id is None:
            raise ValueError("La substitució no té treballador substitut")
        conflicts = _conflictes_substitut(
            conn,
            proposta["treballador_id"],
            substitute_id,
            start,
            end,
        )
        if conflicts:
            conflict_dates = ", ".join(
                sorted({conflict["data"] for conflict in conflicts})
            )
            raise ValueError(
                "No es pot aprovar la substitució: el substitut té una "
                f"indisponibilitat els dies {conflict_dates}."
            )
        current_day = start
        while current_day <= end:
            result_day = _aplica_substitucio_dia(
                conn,
                proposta["treballador_id"],
                substitute_id,
                current_day,
                f"Incidència aprovada #{proposta['incidencia_id']}",
                f"incidencia:{proposta['incidencia_id']}",
            )
            substitute_days_activated += int(
                result_day["descansos_substitut_retirats"] > 0
            )
            days += 1
            current_day += timedelta(days=1)
    else:
        origin = (
            "baixa"
            if proposta["tipus"] in ("baixa", "prorroga_baixa")
            else "temporal"
        )
        current_day = start
        while current_day <= end:
            conn.execute(
                """
                INSERT OR IGNORE INTO descansos_dies
                (treballador_id, data, origen, motiu, treballador_substitut_id)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (
                    proposta["treballador_id"],
                    current_day.isoformat(),
                    origin,
                    f"Incidència aprovada #{proposta['incidencia_id']}",
                ),
            )
            days += 1
            current_day += timedelta(days=1)

    originals = {
        assignment_id: rows_by_id[assignment_id]
        for assignment_id in reemplaçades
    }
    for assignment_id, original in originals.items():
        cursor = conn.execute(
            """
            UPDATE assig_grup_T
            SET estat_planificacio = 'anul_lada'
            WHERE id = ?
              AND estat_planificacio IN ('publicada', 'bloquejada')
            """,
            (assignment_id,),
        )
        if cursor.rowcount != 1:
            raise ValueError(
                f"No s'ha pogut anul·lar l'assignació #{assignment_id}"
            )
        conn.execute(
            """
            DELETE FROM historic_assignacions
            WHERE CAST(treballador_id AS TEXT) = CAST(? AS TEXT)
              AND data = ? AND torn_id = ?
            """,
            (
                original["treballador_id"],
                original["data"],
                original["torn"],
            ),
        )

    published = 0
    for assignment_id, proposed_change in proposades.items():
        original = originals[assignment_id]
        worker = conn.execute(
            """
            SELECT id, treballador, plaza, rotacio, zona, grup
            FROM treballadors
            WHERE CAST(id AS TEXT) = CAST(? AS TEXT)
            """,
            (proposed_change["treballador_id"],),
        ).fetchone()
        if worker is None or worker["grup"] != "T":
            raise ValueError(
                f"Treballador proposat invàlid: "
                f"{proposed_change['treballador_id']}"
            )
        zone_change = int(
            bool(
                worker["zona"]
                and original["zona"]
                and worker["zona"] != original["zona"]
            )
        )
        turn_change = _es_canvi_torn(
            conn,
            worker["rotacio"],
            original["data"],
            original["torn"],
        )
        historic_hours = conn.execute(
            """
            SELECT COALESCE(SUM(durada_hores), 0)
            FROM historic_assignacions
            WHERE CAST(treballador_id AS TEXT) = CAST(? AS TEXT)
            """,
            (worker["id"],),
        ).fetchone()[0]
        new_assignment_id = int(
            conn.execute(
                """
                INSERT INTO assig_grup_T
                (data, dia_setmana, torn, treballador_id, treballador_nom,
                 treballador_plaza, treballador_grup, hora_inici, hora_fi,
                 durada_hores, linia, zona, formacio, es_canvi_zona,
                 es_canvi_torn, hores_totals_any, estat_planificacio)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'publicada')
                """,
                (
                    original["data"],
                    original["dia_setmana"],
                    original["torn"],
                    str(worker["id"]),
                    worker["treballador"],
                    worker["plaza"],
                    worker["grup"],
                    original["hora_inici"],
                    original["hora_fi"],
                    original["durada_hores"],
                    original["linia"],
                    original["zona"],
                    original["formacio"],
                    zone_change,
                    turn_change,
                    float(historic_hours or 0)
                    + float(original["durada_hores"] or 0),
                ),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO historic_assignacions
            (treballador_id, torn_id, data, hora_inici, hora_fi,
             durada_hores, es_canvi_zona, es_canvi_torn, data_apunt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                str(worker["id"]),
                original["torn"],
                original["data"],
                original["hora_inici"],
                original["hora_fi"],
                original["durada_hores"],
                zone_change,
                turn_change,
            ),
        )
        conn.execute(
            """
            UPDATE proposta_canvis
            SET assignacio_nova_id = ?
            WHERE proposta_id = ? AND tipus = 'assignacio_proposada'
              AND assignacio_id = ?
            """,
            (new_assignment_id, proposta_id, assignment_id),
        )
        published += 1

    day_names = ("Dl", "Dt", "Dc", "Dj", "Dv", "Ds", "Dg")
    for need_id, proposed_change in incorporades.items():
        need = need_by_id[need_id]
        if conn.execute(
            """
            SELECT 1 FROM assig_grup_T
            WHERE data = ? AND torn = ?
              AND estat_planificacio IN ('publicada', 'bloquejada')
            LIMIT 1
            """,
            (need.date.isoformat(), need.service_id),
        ).fetchone():
            raise ValueError(
                f"La necessitat {need_id} ja ha estat coberta per un altre procés"
            )
        conn.execute(
            """
            DELETE FROM historic_assignacions
            WHERE data = ? AND torn_id = ?
            """,
            (need.date.isoformat(), need.service_id),
        )
        worker = conn.execute(
            """
            SELECT id, treballador, plaza, rotacio, zona, grup
            FROM treballadors
            WHERE CAST(id AS TEXT) = CAST(? AS TEXT)
            """,
            (proposed_change["treballador_id"],),
        ).fetchone()
        if worker is None or worker["grup"] != "T":
            raise ValueError(
                f"Treballador proposat invàlid: "
                f"{proposed_change['treballador_id']}"
            )
        coverage = conn.execute(
            """
            SELECT linia, zona, formacio
            FROM cobertura
            WHERE data = ? AND servei = ?
            LIMIT 1
            """,
            (need.date.isoformat(), need.service_id),
        ).fetchone()
        line = coverage["linia"] if coverage else ""
        zone = (
            coverage["zona"]
            if coverage and coverage["zona"] is not None
            else need.zone
        )
        skills = (
            coverage["formacio"]
            if coverage and coverage["formacio"] is not None
            else ",".join(sorted(need.required_skills))
        )
        zone_change = int(
            bool(worker["zona"] and zone and worker["zona"] != zone)
        )
        turn_change = _es_canvi_torn(
            conn,
            worker["rotacio"],
            need.date.isoformat(),
            need.service_id,
        )
        historic_hours = conn.execute(
            """
            SELECT COALESCE(SUM(durada_hores), 0)
            FROM historic_assignacions
            WHERE CAST(treballador_id AS TEXT) = CAST(? AS TEXT)
            """,
            (worker["id"],),
        ).fetchone()[0]
        duration_hours = need.duration_minutes / 60
        new_assignment_id = int(
            conn.execute(
                """
                INSERT INTO assig_grup_T
                (data, dia_setmana, torn, treballador_id, treballador_nom,
                 treballador_plaza, treballador_grup, hora_inici, hora_fi,
                 durada_hores, linia, zona, formacio, es_canvi_zona,
                 es_canvi_torn, hores_totals_any, estat_planificacio)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'publicada')
                """,
                (
                    need.date.isoformat(),
                    day_names[need.date.weekday()],
                    need.service_id,
                    str(worker["id"]),
                    worker["treballador"],
                    worker["plaza"],
                    worker["grup"],
                    need.start.strftime("%H:%M"),
                    need.end.strftime("%H:%M"),
                    duration_hours,
                    line,
                    zone,
                    skills,
                    zone_change,
                    turn_change,
                    float(historic_hours or 0) + duration_hours,
                ),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO historic_assignacions
            (treballador_id, torn_id, data, hora_inici, hora_fi,
             durada_hores, es_canvi_zona, es_canvi_torn, data_apunt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                str(worker["id"]),
                need.service_id,
                need.date.isoformat(),
                need.start.strftime("%H:%M"),
                need.end.strftime("%H:%M"),
                duration_hours,
                zone_change,
                turn_change,
            ),
        )
        conn.execute(
            """
            UPDATE proposta_canvis
            SET assignacio_nova_id = ?
            WHERE proposta_id = ? AND tipus = 'assignacio_proposada'
              AND assignacio_id IS NULL AND necessitat_id = ?
            """,
            (new_assignment_id, proposta_id, need_id),
        )
        published += 1

    conn.execute(
        """
        UPDATE propostes_replanificacio
        SET estat = 'aprovada', aprovada_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (proposta_id,),
    )
    conn.execute(
        "UPDATE incidencies_personal SET estat = 'aprovada' WHERE id = ?",
        (proposta["incidencia_id"],),
    )
    _audita(
        conn,
        "proposta",
        proposta_id,
        "aprovada_cp_sat",
        (
            f"snapshot={current_hash}; {days} dies aplicats; "
            f"{len(reemplaçades)} assignacions anul·lades; "
            f"{published} assignacions publicades; "
            f"{len(pendents) + len(descobertes_noves)} serveis descoberts"
        ),
    )
    return {
        "dies_incidencia": days,
        "assignacions_anullades": len(reemplaçades),
        "assignacions_publicades": published,
        "serveis_descoberts": len(pendents) + len(descobertes_noves),
        "dies_substitut_activats": substitute_days_activated,
    }


def aprovar_proposta(db_path: str | Path, proposta_id: int) -> dict[str, int]:
    """Aplica una proposta aprovada: registra el descans i anul·la només el pla afectat."""
    inicialitza_planificacio(db_path)
    with _connexio(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        proposta = conn.execute(
            """SELECT p.*, i.treballador_id, i.treballador_substitut_id, i.tipus, i.data_inici, i.data_fi
               FROM propostes_replanificacio p JOIN incidencies_personal i ON i.id = p.incidencia_id
               WHERE p.id = ?""", (proposta_id,)
        ).fetchone()
        if not proposta or proposta["estat"] != "esborrany":
            raise ValueError("La proposta no està disponible per aprovar")
        if proposta["motor"] == "cp_sat":
            return _aprovar_proposta_cp_sat(conn, proposta, proposta_id)
        if proposta["tipus"] == "alta_anticipada":
            cursor = conn.execute(
                "DELETE FROM descansos_dies WHERE treballador_id = ? AND origen = 'baixa' AND data >= ?",
                (proposta["treballador_id"], proposta["data_inici"]),
            )
            conn.execute("UPDATE propostes_replanificacio SET estat = 'aprovada', aprovada_at = CURRENT_TIMESTAMP WHERE id = ?", (proposta_id,))
            conn.execute("UPDATE incidencies_personal SET estat = 'aprovada' WHERE id = ?", (proposta["incidencia_id"],))
            _audita(conn, "proposta", proposta_id, "aprovada", f"Alta anticipada: {cursor.rowcount} dies de baixa retirats")
            return {
                "dies_incidencia": cursor.rowcount,
                "assignacions_anullades": 0,
                "dies_substitut_activats": 0,
            }

        origen = "baixa" if proposta["tipus"] in ("baixa", "prorroga_baixa") else ("substitucio" if proposta["tipus"] == "substitucio" else "temporal")
        inici = date.fromisoformat(proposta["data_inici"])
        fi = date.fromisoformat(proposta["data_fi"])
        if proposta["tipus"] == "substitucio":
            conflictes = _conflictes_substitut(
                conn,
                proposta["treballador_id"],
                proposta["treballador_substitut_id"],
                inici,
                fi,
            )
            if conflictes:
                dates = ", ".join(
                    sorted({conflicte["data"] for conflicte in conflictes})
                )
                raise ValueError(
                    "No es pot aprovar la substitució: el substitut té una "
                    f"incidència o una altra cobertura activa els dies {dates}."
                )

        dia, dies, dies_substitut_activats = inici, 0, 0
        while dia <= fi:
            motiu = f"Incidència aprovada #{proposta['incidencia_id']}"
            if proposta["tipus"] == "substitucio":
                resultat_dia = _aplica_substitucio_dia(
                    conn,
                    proposta["treballador_id"],
                    proposta["treballador_substitut_id"],
                    dia,
                    motiu,
                    f"incidencia:{proposta['incidencia_id']}",
                )
                if resultat_dia["descansos_substitut_retirats"]:
                    dies_substitut_activats += 1
            else:
                conn.execute(
                    """INSERT OR IGNORE INTO descansos_dies
                       (treballador_id, data, origen, motiu, treballador_substitut_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        proposta["treballador_id"],
                        dia.isoformat(),
                        origen,
                        motiu,
                        proposta["treballador_substitut_id"],
                    ),
                )
            dies += 1
            dia += timedelta(days=1)
        cursor = conn.execute(
            """UPDATE assig_grup_T SET estat_planificacio = 'anul_lada'
               WHERE id IN (SELECT assignacio_id FROM proposta_canvis
                            WHERE proposta_id = ? AND tipus = 'assignacio_a_reemplaçar')""",
            (proposta_id,),
        )
        conn.execute("UPDATE propostes_replanificacio SET estat = 'aprovada', aprovada_at = CURRENT_TIMESTAMP WHERE id = ?", (proposta_id,))
        conn.execute("UPDATE incidencies_personal SET estat = 'aprovada' WHERE id = ?", (proposta["incidencia_id"],))
        _audita(
            conn,
            "proposta",
            proposta_id,
            "aprovada",
            (
                f"{dies} dies aplicats; {cursor.rowcount} assignacions anul·lades; "
                f"{dies_substitut_activats} dies de descans del substitut ajustats"
            ),
        )
    return {
        "dies_incidencia": dies,
        "assignacions_anullades": cursor.rowcount,
        "dies_substitut_activats": dies_substitut_activats,
    }
