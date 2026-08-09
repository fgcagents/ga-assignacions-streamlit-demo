"""Operacions de dades per a descansos, baixes i substitucions.

Aquest mòdul no conté cap entrada ni sortida de consola. Tant Streamlit com
la futura automatització poden reutilitzar les mateixes operacions segures.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


ORIGENS_EDITABLES = ("manual", "temporal", "baixa")


@contextmanager
def _connexio(db_path: str | Path):
    ruta = Path(db_path)
    if not ruta.is_file():
        raise FileNotFoundError(f"No s'ha trobat la base de dades: {ruta}")
    connexio = sqlite3.connect(ruta)
    connexio.row_factory = sqlite3.Row
    try:
        yield connexio
        connexio.commit()
    except Exception:
        connexio.rollback()
        raise
    finally:
        connexio.close()


def _files(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _inicialitza_ajustos_substitucio(conn: sqlite3.Connection) -> None:
    """Crea el registre reversible dels descansos transformats."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ajustos_descans_substitucio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            treballador_original_id INTEGER NOT NULL,
            treballador_substitut_id INTEGER NOT NULL,
            data TEXT NOT NULL,
            rol TEXT NOT NULL CHECK (rol IN ('original', 'substitut')),
            origen_anterior TEXT,
            motiu_anterior TEXT,
            substitut_anterior TEXT,
            referencia TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_ajustos_substitucio_clau
        ON ajustos_descans_substitucio
        (treballador_original_id, treballador_substitut_id, data);
        """
    )


def _conflictes_substitut(
    conn: sqlite3.Connection,
    treballador_original_id: int | str,
    treballador_substitut_id: int | str,
    data_inici: date,
    data_fi: date,
) -> list[dict[str, Any]]:
    """Retorna incidències no base i altres substitucions del substitut."""
    descansos = _files(conn.execute(
        """
        SELECT data, origen, motiu
        FROM descansos_dies
        WHERE treballador_id = ? AND data BETWEEN ? AND ?
          AND COALESCE(origen, '') <> 'base'
        ORDER BY data
        """,
        (
            treballador_substitut_id,
            data_inici.isoformat(),
            data_fi.isoformat(),
        ),
    ).fetchall())
    ocupacions = _files(conn.execute(
        """
        SELECT data, 'substitucio_activa' AS origen,
               'Ja cobreix un altre treballador' AS motiu
        FROM descansos_dies
        WHERE treballador_substitut_id = ? AND data BETWEEN ? AND ?
          AND treballador_id <> ?
        ORDER BY data
        """,
        (
            treballador_substitut_id,
            data_inici.isoformat(),
            data_fi.isoformat(),
            treballador_original_id,
        ),
    ).fetchall())
    return descansos + ocupacions


def _desa_estat_descans(
    conn: sqlite3.Connection,
    treballador_original_id: int | str,
    treballador_substitut_id: int | str,
    data_str: str,
    rol: str,
    files: list[sqlite3.Row],
    referencia: str,
) -> None:
    for fila in files:
        conn.execute(
            """
            INSERT INTO ajustos_descans_substitucio
            (treballador_original_id, treballador_substitut_id, data, rol,
             origen_anterior, motiu_anterior, substitut_anterior, referencia)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                treballador_original_id,
                treballador_substitut_id,
                data_str,
                rol,
                fila["origen"],
                fila["motiu"],
                fila["treballador_substitut_id"],
                referencia,
            ),
        )


def _aplica_substitucio_dia(
    conn: sqlite3.Connection,
    treballador_original_id: int | str,
    treballador_substitut_id: int | str,
    dia: date,
    motiu: str,
    referencia: str,
) -> dict[str, int]:
    """Normalitza original i substitut i conserva l'estat anterior."""
    _inicialitza_ajustos_substitucio(conn)
    data_str = dia.isoformat()
    ajust_existent = conn.execute(
        """
        SELECT 1 FROM ajustos_descans_substitucio
        WHERE treballador_original_id = ? AND treballador_substitut_id = ?
          AND data = ? LIMIT 1
        """,
        (treballador_original_id, treballador_substitut_id, data_str),
    ).fetchone()
    substitucio_normalitzada = conn.execute(
        """
        SELECT 1 FROM descansos_dies
        WHERE treballador_id = ? AND treballador_substitut_id = ?
          AND data = ? AND origen = 'substitucio' LIMIT 1
        """,
        (treballador_original_id, treballador_substitut_id, data_str),
    ).fetchone()
    if ajust_existent and substitucio_normalitzada:
        return {"afegit": 0, "descansos_substitut_retirats": 0}

    files_original = conn.execute(
        """
        SELECT origen, motiu, treballador_substitut_id
        FROM descansos_dies
        WHERE treballador_id = ? AND data = ?
          AND NOT (
              origen = 'substitucio'
              AND CAST(treballador_substitut_id AS TEXT) = CAST(? AS TEXT)
          )
        """,
        (treballador_original_id, data_str, treballador_substitut_id),
    ).fetchall()
    files_substitut = conn.execute(
        """
        SELECT origen, motiu, treballador_substitut_id
        FROM descansos_dies WHERE treballador_id = ? AND data = ?
        """,
        (treballador_substitut_id, data_str),
    ).fetchall()

    _desa_estat_descans(
        conn, treballador_original_id, treballador_substitut_id,
        data_str, "original", files_original, referencia,
    )
    _desa_estat_descans(
        conn, treballador_original_id, treballador_substitut_id,
        data_str, "substitut", files_substitut, referencia,
    )

    conn.execute(
        "DELETE FROM descansos_dies WHERE treballador_id = ? AND data = ?",
        (treballador_original_id, data_str),
    )
    conn.execute(
        "DELETE FROM descansos_dies WHERE treballador_id = ? AND data = ?",
        (treballador_substitut_id, data_str),
    )
    conn.execute(
        """
        INSERT INTO descansos_dies
        (treballador_id, data, origen, motiu, treballador_substitut_id)
        VALUES (?, ?, 'substitucio', ?, ?)
        """,
        (
            treballador_original_id,
            data_str,
            motiu or None,
            treballador_substitut_id,
        ),
    )
    return {
        "afegit": 1,
        "descansos_substitut_retirats": len(files_substitut),
    }


def _restaura_ajustos_substitucio(
    conn: sqlite3.Connection,
    treballador_original_id: int | str,
    treballador_substitut_id: int | str,
    dates: list[str],
) -> int:
    """Restaura els descansos previs quan desapareix una substitució."""
    _inicialitza_ajustos_substitucio(conn)
    restaurats = 0
    for data_str in dates:
        ajustos = conn.execute(
            """
            SELECT * FROM ajustos_descans_substitucio
            WHERE treballador_original_id = ? AND treballador_substitut_id = ?
              AND data = ? ORDER BY id
            """,
            (treballador_original_id, treballador_substitut_id, data_str),
        ).fetchall()
        substitut_encara_ocupat = conn.execute(
            """
            SELECT 1 FROM descansos_dies
            WHERE treballador_substitut_id = ? AND data = ? LIMIT 1
            """,
            (treballador_substitut_id, data_str),
        ).fetchone()
        for ajust in ajustos:
            if ajust["rol"] == "substitut" and substitut_encara_ocupat:
                continue
            treballador_id = (
                treballador_original_id
                if ajust["rol"] == "original"
                else treballador_substitut_id
            )
            conn.execute(
                """
                INSERT INTO descansos_dies
                (treballador_id, data, origen, motiu, treballador_substitut_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    treballador_id,
                    data_str,
                    ajust["origen_anterior"],
                    ajust["motiu_anterior"],
                    ajust["substitut_anterior"],
                ),
            )
            restaurats += 1
        conn.execute(
            """
            DELETE FROM ajustos_descans_substitucio
            WHERE treballador_original_id = ? AND treballador_substitut_id = ?
              AND data = ?
            """,
            (treballador_original_id, treballador_substitut_id, data_str),
        )
    return restaurats


def llista_treballadors(db_path: str | Path) -> list[dict[str, Any]]:
    with _connexio(db_path) as conn:
        files = conn.execute(
            """
            SELECT id, treballador, plaza, rotacio, zona, grup, habilitacions
            FROM treballadors
            ORDER BY treballador
            """
        ).fetchall()
    return _files(files)


def cerca_treballadors(db_path: str | Path, cerca: str) -> list[dict[str, Any]]:
    terme = f"%{cerca.strip()}%"
    with _connexio(db_path) as conn:
        files = conn.execute(
            """
            SELECT id, treballador, plaza, rotacio, zona, grup, habilitacions
            FROM treballadors
            WHERE CAST(id AS TEXT) LIKE ? OR treballador LIKE ? OR plaza LIKE ?
            ORDER BY treballador
            """,
            (terme, terme, terme),
        ).fetchall()
    return _files(files)


def moviments_treballador(
    db_path: str | Path, treballador_id: int | str, any_: int | None = None
) -> list[dict[str, Any]]:
    consulta = """
        SELECT d.id, d.data, d.origen, d.motiu, d.treballador_id,
               d.treballador_substitut_id, original.treballador AS nom_original,
               original.plaza AS plaza_original, substitut.treballador AS nom_substitut,
               substitut.plaza AS plaza_substitut,
               CASE WHEN d.treballador_id = ? THEN 'Original' ELSE 'Substitut' END AS rol
        FROM descansos_dies d
        JOIN treballadors original ON original.id = d.treballador_id
        LEFT JOIN treballadors substitut ON substitut.id = d.treballador_substitut_id
        WHERE d.treballador_id = ? OR d.treballador_substitut_id = ?
    """
    params: list[Any] = [treballador_id, treballador_id, treballador_id]
    if any_ is not None:
        consulta += " AND substr(d.data, 1, 4) = ?"
        params.append(str(any_))
    consulta += " ORDER BY d.data, d.id"
    with _connexio(db_path) as conn:
        files = conn.execute(consulta, params).fetchall()
    return _files(files)


def afegir_periode(
    db_path: str | Path,
    treballador_id: int | str,
    data_inici: date,
    data_fi: date,
    origen: str,
    motiu: str = "",
) -> dict[str, int]:
    if origen not in ORIGENS_EDITABLES:
        raise ValueError("Origen de descans no admès")
    if data_fi < data_inici:
        raise ValueError("La data final no pot ser anterior a la inicial")

    files = [
        (treballador_id, (data_inici + timedelta(days=offset)).isoformat(), origen, motiu or None)
        for offset in range((data_fi - data_inici).days + 1)
    ]
    with _connexio(db_path) as conn:
        cursor = conn.cursor()
        afegits = 0
        for fila in files:
            cursor.execute(
                """
                INSERT OR IGNORE INTO descansos_dies
                (treballador_id, data, origen, motiu)
                VALUES (?, ?, ?, ?)
                """,
                fila,
            )
            afegits += cursor.rowcount
    return {"afegits": afegits, "existents": len(files) - afegits}


def eliminar_periode(
    db_path: str | Path,
    treballador_id: int | str,
    data_inici: date,
    data_fi: date,
    origen: str,
) -> int:
    if origen not in ORIGENS_EDITABLES:
        raise ValueError("Només es poden eliminar descansos manuals, temporals o baixes")
    if data_fi < data_inici:
        raise ValueError("La data final no pot ser anterior a la inicial")
    with _connexio(db_path) as conn:
        cursor = conn.execute(
            """
            DELETE FROM descansos_dies
            WHERE treballador_id = ? AND data BETWEEN ? AND ? AND origen = ?
            """,
            (treballador_id, data_inici.isoformat(), data_fi.isoformat(), origen),
        )
    return cursor.rowcount


def crear_substitucio(
    db_path: str | Path,
    treballador_original_id: int | str,
    treballador_substitut_id: int | str,
    data_inici: date,
    data_fi: date,
    motiu: str = "",
    permet_conflictes: bool = False,
) -> dict[str, Any]:
    if str(treballador_original_id) == str(treballador_substitut_id):
        raise ValueError("Un treballador no es pot substituir a si mateix")
    if data_fi < data_inici:
        raise ValueError("La data final no pot ser anterior a la inicial")

    with _connexio(db_path) as conn:
        originals = conn.execute(
            "SELECT id FROM treballadors WHERE id IN (?, ?)",
            (treballador_original_id, treballador_substitut_id),
        ).fetchall()
        if len(originals) != 2:
            raise ValueError("El treballador original o el substitut no existeixen")

        conflictes = _conflictes_substitut(
            conn,
            treballador_original_id,
            treballador_substitut_id,
            data_inici,
            data_fi,
        )
        if conflictes and not permet_conflictes:
            return {"afegits": 0, "existents": 0, "conflictes": conflictes}

        afegits = 0
        descansos_substitut_retirats = 0
        total_dies = (data_fi - data_inici).days + 1
        referencia = (
            f"directa:{treballador_original_id}:{treballador_substitut_id}:"
            f"{data_inici.isoformat()}:{data_fi.isoformat()}:{motiu}"
        )
        for offset in range(total_dies):
            resultat = _aplica_substitucio_dia(
                conn,
                treballador_original_id,
                treballador_substitut_id,
                data_inici + timedelta(days=offset),
                motiu,
                referencia,
            )
            afegits += resultat["afegit"]
            descansos_substitut_retirats += resultat[
                "descansos_substitut_retirats"
            ]
    return {
        "afegits": afegits,
        "existents": total_dies - afegits,
        "conflictes": conflictes,
        "descansos_substitut_retirats": descansos_substitut_retirats,
    }


def llista_substitucions(db_path: str | Path) -> list[dict[str, Any]]:
    with _connexio(db_path) as conn:
        files = conn.execute(
            """
            SELECT d.treballador_id AS original_id, original.treballador AS original,
                   original.plaza AS plaza_original,
                   d.treballador_substitut_id AS substitut_id,
                   substitut.treballador AS substitut, substitut.plaza AS plaza_substitut,
                   MIN(d.data) AS data_inici, MAX(d.data) AS data_fi,
                   COUNT(*) AS dies, d.motiu
            FROM descansos_dies d
            JOIN treballadors original ON original.id = d.treballador_id
            JOIN treballadors substitut ON substitut.id = d.treballador_substitut_id
            WHERE d.origen = 'substitucio'
            GROUP BY d.treballador_id, d.treballador_substitut_id, d.motiu
            ORDER BY data_inici, original
            """
        ).fetchall()
    return _files(files)


def eliminar_substitucio(
    db_path: str | Path,
    treballador_original_id: int | str,
    treballador_substitut_id: int | str,
    data_inici: str,
    data_fi: str,
    motiu: str | None,
) -> int:
    with _connexio(db_path) as conn:
        dates = [
            fila["data"] for fila in conn.execute(
                """
                SELECT DISTINCT data FROM descansos_dies
                WHERE treballador_id = ? AND treballador_substitut_id = ?
                  AND data BETWEEN ? AND ? AND origen = 'substitucio'
                  AND (motiu = ? OR (motiu IS NULL AND ? IS NULL))
                ORDER BY data
                """,
                (
                    treballador_original_id,
                    treballador_substitut_id,
                    data_inici,
                    data_fi,
                    motiu,
                    motiu,
                ),
            ).fetchall()
        ]
        cursor = conn.execute(
            """
            DELETE FROM descansos_dies
            WHERE treballador_id = ? AND treballador_substitut_id = ?
              AND data BETWEEN ? AND ? AND origen = 'substitucio'
              AND (motiu = ? OR (motiu IS NULL AND ? IS NULL))
            """,
            (treballador_original_id, treballador_substitut_id, data_inici, data_fi, motiu, motiu),
        )
        _restaura_ajustos_substitucio(
            conn,
            treballador_original_id,
            treballador_substitut_id,
            dates,
        )
    return cursor.rowcount


def repara_substitucions_existents(db_path: str | Path) -> dict[str, int]:
    """Normalitza substitucions antigues quan el substitut només té descans base."""
    reparades = 0
    descansos_substitut_retirats = 0
    omeses = 0
    with _connexio(db_path) as conn:
        substitucions = conn.execute(
            """
            SELECT DISTINCT treballador_id, treballador_substitut_id, data, motiu
            FROM descansos_dies
            WHERE origen = 'substitucio'
              AND treballador_substitut_id IS NOT NULL
            ORDER BY data
            """
        ).fetchall()
        for substitucio in substitucions:
            dia = date.fromisoformat(substitucio["data"])
            conflictes = _conflictes_substitut(
                conn,
                substitucio["treballador_id"],
                substitucio["treballador_substitut_id"],
                dia,
                dia,
            )
            if conflictes:
                omeses += 1
                continue
            resultat = _aplica_substitucio_dia(
                conn,
                substitucio["treballador_id"],
                substitucio["treballador_substitut_id"],
                dia,
                substitucio["motiu"] or "",
                "reparacio_substitucio_existent",
            )
            reparades += resultat["afegit"]
            descansos_substitut_retirats += resultat[
                "descansos_substitut_retirats"
            ]
    return {
        "dies_reparats": reparades,
        "descansos_substitut_retirats": descansos_substitut_retirats,
        "dies_omesos_per_conflicte": omeses,
    }


def disponibilitat_dia(db_path: str | Path, dia: date) -> dict[str, list[dict[str, Any]]]:
    data_str = dia.isoformat()
    with _connexio(db_path) as conn:
        descansos = _files(conn.execute(
            """
            SELECT original.id, original.treballador, original.plaza, original.rotacio,
                   original.zona, d.origen, d.motiu, substitut.treballador AS nom_substitut,
                   substitut.plaza AS plaza_substitut
            FROM descansos_dies d
            JOIN treballadors original ON original.id = d.treballador_id
            LEFT JOIN treballadors substitut ON substitut.id = d.treballador_substitut_id
            WHERE d.data = ?
            ORDER BY original.rotacio, original.treballador
            """,
            (data_str,),
        ).fetchall())
        # Igual que a la consulta de consola: aquesta pantalla informa de la
        # disponibilitat de la plaça. Un substitut continua disponible com a
        # treballador, encara que aquell dia cobreixi una altra plaça.
        ids_no_disponibles = {fila['id'] for fila in descansos}
        treballadors = _files(conn.execute(
            """
            SELECT id, treballador, plaza, rotacio, zona, grup
            FROM treballadors ORDER BY rotacio, treballador
            """
        ).fetchall())

    disponibles = [fila for fila in treballadors if fila['id'] not in ids_no_disponibles]
    return {"disponibles": disponibles, "descansos": descansos}


def resum_mensual(db_path: str | Path, any_: int, mes: int) -> list[dict[str, Any]]:
    inici = date(any_, mes, 1)
    fi = date(any_ + 1, 1, 1) - timedelta(days=1) if mes == 12 else date(any_, mes + 1, 1) - timedelta(days=1)
    with _connexio(db_path) as conn:
        total_treballadors = conn.execute("SELECT COUNT(*) FROM treballadors").fetchone()[0]
        files = conn.execute(
            """
            SELECT data, COUNT(*) AS places_no_disponibles
            FROM descansos_dies
            WHERE data BETWEEN ? AND ?
            GROUP BY data ORDER BY data
            """,
            (inici.isoformat(), fi.isoformat()),
        ).fetchall()
    per_dia = {fila['data']: fila['places_no_disponibles'] for fila in files}
    resultat = []
    dia = inici
    while dia <= fi:
        num = per_dia.get(dia.isoformat(), 0)
        resultat.append({
            "data": dia.isoformat(),
            "places_no_disponibles": num,
            "percentatge": round(num / total_treballadors * 100, 1) if total_treballadors else 0,
        })
        dia += timedelta(days=1)
    return resultat


def historial_canvis(db_path: str | Path, limit: int = 100) -> list[dict[str, Any]]:
    with _connexio(db_path) as conn:
        files = conn.execute(
            """
            SELECT d.id, d.data, d.origen, d.motiu, original.treballador AS treballador,
                   original.plaza, substitut.treballador AS substitut
            FROM descansos_dies d
            JOIN treballadors original ON original.id = d.treballador_id
            LEFT JOIN treballadors substitut ON substitut.id = d.treballador_substitut_id
            WHERE d.origen IN ('manual', 'temporal', 'baixa', 'substitucio')
            ORDER BY d.id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return _files(files)


def detectar_serveis_descoberts(
    db_path: str | Path, data_inici: date, data_fi: date
) -> list[dict[str, Any]]:
    """Retorna serveis sense cap plaça efectiva disponible dins d'un període."""
    if data_fi < data_inici:
        raise ValueError("La data final no pot ser anterior a la inicial")
    with _connexio(db_path) as conn:
        serveis = _files(conn.execute(
            "SELECT servei, opcio_1, opcio_2 FROM serveis ORDER BY servei"
        ).fetchall())
        treballadors = _files(conn.execute(
            "SELECT id, plaza FROM treballadors"
        ).fetchall())
        id_per_placa = {fila["plaza"]: fila["id"] for fila in treballadors}
        descansos = _files(conn.execute(
            """
            SELECT treballador_id, treballador_substitut_id, data
            FROM descansos_dies WHERE data BETWEEN ? AND ?
            """,
            (data_inici.isoformat(), data_fi.isoformat()),
        ).fetchall())

    cobertura = {
        (fila["treballador_id"], fila["data"]): fila["treballador_substitut_id"]
        for fila in descansos
    }

    def estat_placa(placa: str | None, data_str: str) -> tuple[int | None, str]:
        if not placa:
            return None, "Sense plaça configurada"
        treballador_id = id_per_placa.get(placa)
        if treballador_id is None:
            return None, "Plaça no trobada a la base de dades"
        clau = (treballador_id, data_str)
        if clau not in cobertura:
            return treballador_id, "Disponible"
        substitut_id = cobertura[clau]
        if substitut_id is not None:
            return substitut_id, f"Substituït per ID {substitut_id}"
        return None, "Té descans sense substitut"

    resultats = []
    data_actual = data_inici
    while data_actual <= data_fi:
        data_str = data_actual.isoformat()
        for servei in serveis:
            efectiu_1, motiu_1 = estat_placa(servei["opcio_1"], data_str)
            efectiu_2, motiu_2 = estat_placa(servei["opcio_2"], data_str)
            if efectiu_1 is None and efectiu_2 is None:
                resultats.append({
                    "data": data_str,
                    "servei": servei["servei"],
                    "opcio_1": servei["opcio_1"],
                    "motiu_opcio_1": motiu_1,
                    "opcio_2": servei["opcio_2"],
                    "motiu_opcio_2": motiu_2,
                })
        data_actual += timedelta(days=1)
    return resultats


def estadistiques_descansos(
    db_path: str | Path, any_: int | None = None
) -> list[dict[str, Any]]:
    consulta = """
        SELECT t.id, t.treballador, t.plaza, t.rotacio, t.zona,
               COUNT(d.id) AS total_descansos
        FROM treballadors t
        LEFT JOIN descansos_dies d ON t.id = d.treballador_id
    """
    params: list[Any] = []
    if any_ is not None:
        consulta += " AND substr(d.data, 1, 4) = ?"
        params.append(str(any_))
    consulta += " GROUP BY t.id ORDER BY total_descansos DESC, t.treballador"
    with _connexio(db_path) as conn:
        files = conn.execute(consulta, params).fetchall()
    return _files(files)


def alertes_baixes_pendents(
    db_path: str | Path, dies_marge: int = 7, avui: date | None = None
) -> list[dict[str, Any]]:
    avui = avui or date.today()
    data_limit = avui + timedelta(days=dies_marge)
    with _connexio(db_path) as conn:
        files = conn.execute(
            """
            WITH ultima_baixa AS (
                SELECT treballador_id, MAX(data) AS data_fi_prevista, MAX(motiu) AS motiu
                FROM descansos_dies
                WHERE origen = 'baixa'
                GROUP BY treballador_id
            )
            SELECT t.id, t.treballador, t.plaza, ub.data_fi_prevista, ub.motiu
            FROM treballadors t
            JOIN ultima_baixa ub ON ub.treballador_id = t.id
            WHERE ub.data_fi_prevista <= ?
            ORDER BY ub.data_fi_prevista, t.treballador
            """,
            (data_limit.isoformat(),),
        ).fetchall()
    resultats = _files(files)
    for fila in resultats:
        data_fi = date.fromisoformat(fila["data_fi_prevista"])
        dies = (data_fi - avui).days
        fila["estat"] = "Expirada" if dies < 0 else "Propera"
        fila["dies_fins_fi"] = dies
    return resultats
