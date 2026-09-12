"""Migració idempotent de la persistència genèrica de planificació."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import time
from pathlib import Path


SCHEMA_VERSION = 9
EXECUTION_STATES = (
    "esborrany",
    "validada",
    "publicada",
    "descartada",
    "invalidada",
    "revertida",
)


class PlanningSchemaMigrationError(RuntimeError):
    """Indica que l'esquema no s'ha pogut migrar de manera segura."""


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _parse_service_time(raw_value: object) -> time:
    value = str(raw_value or "").replace('"', "").strip()
    if not value:
        raise PlanningSchemaMigrationError("Hora de servei buida")
    try:
        hours, minutes = (int(part) for part in value.split(":")[:2])
        return time(hours % 24, minutes)
    except (TypeError, ValueError) as error:
        raise PlanningSchemaMigrationError(
            f"Hora de servei invàlida: {raw_value}"
        ) from error


def _is_night_service_window(start: object, end: object) -> bool:
    start_time = _parse_service_time(start)
    end_time = _parse_service_time(end)
    return start_time > time(19, 0) and time.min < end_time <= time(6, 0)


def _migrate_service_night_indicator(
    connection: sqlite3.Connection,
) -> None:
    if not _table_exists(connection, "serveis_horaris"):
        return
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(serveis_horaris)")
    }
    if "is_night" not in columns:
        connection.execute(
            "ALTER TABLE serveis_horaris "
            "ADD COLUMN is_night INTEGER NOT NULL DEFAULT 0 "
            "CHECK (is_night IN (0, 1))"
        )
    for row in connection.execute('SELECT * FROM "serveis_horaris"'):
        flags = [
            _is_night_service_window(
                row[f"Inici S{index}"], row[f"Final S{index}"]
            )
            for index in range(1, 5)
            if row[f"Servei {index}"]
            and row[f"Inici S{index}"]
            and row[f"Final S{index}"]
        ]
        connection.execute(
            'UPDATE "serveis_horaris" SET is_night = ? WHERE Torn = ?',
            (int(any(flags)), str(row["Torn"])),
        )


def _published_plan_snapshot(connection: sqlite3.Connection) -> tuple[str, int]:
    """Retorna una empremta estable i el volum del pla oficial vigent."""
    if not _table_exists(connection, "assig_grup_T"):
        payload: list[dict[str, object]] = []
    else:
        cursor = connection.execute(
            """
            SELECT * FROM assig_grup_T
            WHERE estat_planificacio IN ('publicada', 'bloquejada')
            ORDER BY data, torn, id
            """
        )
        columns = [item[0] for item in cursor.description]
        payload = [dict(zip(columns, row, strict=True)) for row in cursor]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), len(payload)


def register_published_plan_version(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    execution_id: int,
    publication_id: int,
    origin: str,
    origin_id: str | None,
    start_date: str,
    end_date: str,
    snapshot_hash: str | None = None,
) -> int:
    """Registra una versió oficial dins de la transacció del publicador."""
    calculated_hash, active_assignments = _published_plan_snapshot(connection)
    final_hash = snapshot_hash or calculated_hash
    cursor = connection.execute(
        """
        INSERT INTO versions_pla_publicat
        (tipus_event, execucio_id, publicacio_id, origen, origen_id,
         data_inici, data_fi, snapshot_hash, assignacions_actives)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_type,
            execution_id,
            publication_id,
            origin,
            origin_id,
            start_date,
            end_date,
            final_hash,
            active_assignments,
        ),
    )
    return int(cursor.lastrowid)


def _bootstrap_published_plan_version(connection: sqlite3.Connection) -> None:
    """Etiqueta com a V1 l'estat oficial preexistent, sense inventar historial."""
    if not _table_exists(connection, "assig_grup_T"):
        return
    if connection.execute(
        "SELECT 1 FROM versions_pla_publicat LIMIT 1"
    ).fetchone():
        return
    snapshot_hash, active_assignments = _published_plan_snapshot(connection)
    period = connection.execute(
        """
        SELECT MIN(data), MAX(data) FROM assig_grup_T
        WHERE estat_planificacio IN ('publicada', 'bloquejada')
        """
    ).fetchone()
    connection.execute(
        """
        INSERT INTO versions_pla_publicat
        (tipus_event, origen, origen_id, data_inici, data_fi,
         snapshot_hash, assignacions_actives)
        VALUES ('bootstrap', 'sistema', 'estat_inicial_importat', ?, ?, ?, ?)
        """,
        (period[0], period[1], snapshot_hash, active_assignments),
    )


def _repair_legacy_replanning_foreign_key(
    connection: sqlite3.Connection,
) -> bool:
    """Canvia només la referència antiga i conserva definició i files."""
    if not _table_exists(connection, "propostes_replanificacio"):
        return False
    foreign_keys = connection.execute(
        "PRAGMA foreign_key_list(propostes_replanificacio)"
    ).fetchall()
    if not any(row[2] == "incidencies_personal_v1" for row in foreign_keys):
        return False
    if not _table_exists(connection, "incidencies_personal"):
        raise PlanningSchemaMigrationError(
            "No es pot reparar propostes_replanificacio: falta "
            "incidencies_personal"
        )
    definition_row = connection.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type = 'table' AND name = 'propostes_replanificacio'
        """
    ).fetchone()
    if definition_row is None or not definition_row[0]:
        raise PlanningSchemaMigrationError(
            "No s'ha pogut llegir la definició de propostes_replanificacio"
        )
    definition = str(definition_row[0])
    opening = definition.find("(")
    if opening < 0:
        raise PlanningSchemaMigrationError(
            "Definició invàlida de propostes_replanificacio"
        )
    rebuilt_table = "propostes_replanificacio_rebuilt"
    rebuilt_sql = (
        f'CREATE TABLE "{rebuilt_table}" '
        + definition[opening:].replace(
            "incidencies_personal_v1",
            "incidencies_personal",
        )
    )
    columns = [
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(propostes_replanificacio)"
        )
    ]
    quoted_columns = ", ".join(f'"{column}"' for column in columns)
    connection.execute(f'DROP TABLE IF EXISTS "{rebuilt_table}"')
    connection.execute(rebuilt_sql)
    connection.execute(
        f'INSERT INTO "{rebuilt_table}" ({quoted_columns}) '
        f'SELECT {quoted_columns} FROM "propostes_replanificacio"'
    )
    connection.execute('DROP TABLE "propostes_replanificacio"')
    connection.execute(
        f'ALTER TABLE "{rebuilt_table}" RENAME TO "propostes_replanificacio"'
    )
    return True


def _migrate_partial_publications(connection: sqlite3.Connection) -> None:
    """Permet diversos blocs consecutius per una mateixa execució."""
    if not _table_exists(connection, "publicacions_planificacio_cp_sat"):
        return
    columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(publicacions_planificacio_cp_sat)"
        )
    }
    definition_row = connection.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type = 'table' AND name = 'publicacions_planificacio_cp_sat'
        """
    ).fetchone()
    definition = str(definition_row[0] or "") if definition_row else ""
    if {
        "data_inici",
        "data_fi",
    }.issubset(columns) and "execucio_id INTEGER NOT NULL UNIQUE" not in definition:
        return

    rebuilt = "publicacions_planificacio_cp_sat_rebuilt"
    connection.execute(f'DROP TABLE IF EXISTS "{rebuilt}"')
    connection.execute(
        f"""
        CREATE TABLE "{rebuilt}" (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execucio_id INTEGER NOT NULL,
            data_inici TEXT NOT NULL,
            data_fi TEXT NOT NULL,
            snapshot_anterior_hash TEXT NOT NULL,
            snapshot_posterior_hash TEXT NOT NULL,
            affected_anterior_hash TEXT NOT NULL,
            affected_posterior_hash TEXT NOT NULL,
            backup_path TEXT NOT NULL,
            resum_json TEXT NOT NULL,
            rollback_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reverted_at TEXT,
            FOREIGN KEY (execucio_id)
                REFERENCES execucions_planificacio_cp_sat(id),
            CHECK (data_fi >= data_inici)
        )
        """
    )
    start_expression = (
        "p.data_inici" if "data_inici" in columns else "e.data_inici"
    )
    end_expression = "p.data_fi" if "data_fi" in columns else "e.data_fi"
    affected_before_expression = (
        "p.affected_anterior_hash"
        if "affected_anterior_hash" in columns
        else "''"
    )
    affected_after_expression = (
        "p.affected_posterior_hash"
        if "affected_posterior_hash" in columns
        else "''"
    )
    connection.execute(
        f"""
        INSERT INTO "{rebuilt}"
        (id, execucio_id, data_inici, data_fi,
         snapshot_anterior_hash, snapshot_posterior_hash,
         affected_anterior_hash, affected_posterior_hash,
         backup_path, resum_json, rollback_json, created_at, reverted_at)
        SELECT p.id, p.execucio_id, {start_expression}, {end_expression},
               p.snapshot_anterior_hash, p.snapshot_posterior_hash,
               {affected_before_expression}, {affected_after_expression},
               p.backup_path, p.resum_json, p.rollback_json,
               p.created_at, p.reverted_at
        FROM publicacions_planificacio_cp_sat p
        JOIN execucions_planificacio_cp_sat e ON e.id = p.execucio_id
        """
    )
    connection.execute('DROP TABLE "publicacions_planificacio_cp_sat"')
    connection.execute(
        f'ALTER TABLE "{rebuilt}" RENAME TO "publicacions_planificacio_cp_sat"'
    )


def _migrate_invalidated_execution_state(
    connection: sqlite3.Connection,
) -> None:
    """Afegeix l'estat invalidada conservant les execucions existents."""
    if not _table_exists(connection, "execucions_planificacio_cp_sat"):
        return
    definition_row = connection.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type = 'table' AND name = 'execucions_planificacio_cp_sat'
        """
    ).fetchone()
    definition = str(definition_row[0] or "") if definition_row else ""
    if "'invalidada'" in definition:
        return
    if "'revertida'" not in definition:
        raise PlanningSchemaMigrationError(
            "No s'ha pogut ampliar els estats de les propostes"
        )

    rebuilt = "execucions_planificacio_cp_sat_rebuilt"
    rebuilt_sql = definition.replace(
        "execucions_planificacio_cp_sat",
        rebuilt,
        1,
    ).replace("'revertida'", "'invalidada', 'revertida'", 1)
    columns = [
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(execucions_planificacio_cp_sat)"
        )
    ]
    quoted_columns = ", ".join(f'"{column}"' for column in columns)
    connection.execute(f'DROP TABLE IF EXISTS "{rebuilt}"')
    connection.execute(rebuilt_sql)
    connection.execute(
        f'INSERT INTO "{rebuilt}" ({quoted_columns}) '
        f'SELECT {quoted_columns} FROM "execucions_planificacio_cp_sat"'
    )
    connection.execute('DROP TABLE "execucions_planificacio_cp_sat"')
    connection.execute(
        f'ALTER TABLE "{rebuilt}" RENAME TO "execucions_planificacio_cp_sat"'
    )


def _create_generic_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS migracions_planificacio_cp_sat (
            versio INTEGER PRIMARY KEY,
            aplicada_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS execucions_planificacio_cp_sat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            estat TEXT NOT NULL DEFAULT 'esborrany'
                CHECK (estat IN (
                    'esborrany', 'validada', 'publicada',
                    'descartada', 'invalidada', 'revertida'
                )),
            origen TEXT NOT NULL,
            origen_id TEXT,
            motiu TEXT,
            data_inici TEXT NOT NULL,
            data_fi TEXT NOT NULL,
            abast_json TEXT NOT NULL,
            politica_json TEXT NOT NULL,
            configuracio_json TEXT NOT NULL,
            snapshot_hash TEXT NOT NULL,
            problem_hash TEXT NOT NULL,
            solver_status TEXT NOT NULL,
            metriques_json TEXT NOT NULL,
            necessitats_cobertes INTEGER NOT NULL,
            necessitats_totals INTEGER NOT NULL,
            assignacions_conservades INTEGER NOT NULL,
            canvis_persistibles INTEGER NOT NULL,
            necessitats_descobertes INTEGER NOT NULL,
            llavor_seleccionada INTEGER NOT NULL,
            resultat_hash TEXT NOT NULL,
            backup_path TEXT,
            snapshot_final_hash TEXT,
            equity_override_json TEXT,
            equity_override_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            validated_at TEXT,
            published_at TEXT,
            discarded_at TEXT,
            reverted_at TEXT
        )
        """
    )
    execution_columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(execucions_planificacio_cp_sat)"
        )
    }
    for column in ("equity_override_json", "equity_override_at"):
        if column not in execution_columns:
            connection.execute(
                f"ALTER TABLE execucions_planificacio_cp_sat "
                f"ADD COLUMN {column} TEXT"
            )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS canvis_planificacio_cp_sat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execucio_id INTEGER NOT NULL,
            ordre INTEGER NOT NULL,
            tipus TEXT NOT NULL
                CHECK (tipus IN ('alta', 'baixa', 'reassignacio')),
            necessitat_id TEXT NOT NULL,
            data TEXT NOT NULL,
            servei TEXT NOT NULL,
            assignacio_anterior_id INTEGER,
            treballador_anterior_id TEXT,
            assignacio_nova_id INTEGER,
            treballador_nou_id TEXT,
            anterior_json TEXT,
            posterior_json TEXT,
            motiu TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (execucio_id)
                REFERENCES execucions_planificacio_cp_sat(id)
                ON DELETE CASCADE,
            UNIQUE (execucio_id, necessitat_id),
            UNIQUE (execucio_id, ordre)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS bloquejos_planificacio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            necessitat_id TEXT NOT NULL,
            assignacio_id INTEGER,
            origen TEXT NOT NULL,
            origen_id TEXT,
            motiu TEXT NOT NULL,
            vigent_des_de TEXT NOT NULL,
            vigent_fins TEXT,
            estat TEXT NOT NULL DEFAULT 'actiu'
                CHECK (estat IN ('actiu', 'inactiu', 'revocat')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deactivated_at TEXT,
            CHECK (vigent_fins IS NULL OR vigent_fins >= vigent_des_de)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS preassignacions_planificacio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            necessitat_id TEXT NOT NULL,
            data TEXT NOT NULL,
            servei TEXT NOT NULL,
            treballador_id TEXT NOT NULL,
            motiu TEXT NOT NULL,
            estat TEXT NOT NULL DEFAULT 'activa'
                CHECK (estat IN ('activa', 'revocada')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deactivated_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS publicacions_planificacio_cp_sat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execucio_id INTEGER NOT NULL,
            data_inici TEXT NOT NULL,
            data_fi TEXT NOT NULL,
            snapshot_anterior_hash TEXT NOT NULL,
            snapshot_posterior_hash TEXT NOT NULL,
            affected_anterior_hash TEXT NOT NULL,
            affected_posterior_hash TEXT NOT NULL,
            backup_path TEXT NOT NULL,
            resum_json TEXT NOT NULL,
            rollback_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reverted_at TEXT,
            FOREIGN KEY (execucio_id)
                REFERENCES execucions_planificacio_cp_sat(id),
            CHECK (data_fi >= data_inici)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS versions_pla_publicat (
            versio INTEGER PRIMARY KEY AUTOINCREMENT,
            tipus_event TEXT NOT NULL
                CHECK (tipus_event IN ('bootstrap', 'publicacio', 'rollback')),
            execucio_id INTEGER,
            publicacio_id INTEGER,
            origen TEXT NOT NULL,
            origen_id TEXT,
            data_inici TEXT,
            data_fi TEXT,
            snapshot_hash TEXT NOT NULL,
            assignacions_actives INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (execucio_id)
                REFERENCES execucions_planificacio_cp_sat(id),
            FOREIGN KEY (publicacio_id)
                REFERENCES publicacions_planificacio_cp_sat(id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS notificacions_xivato (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            publicacio_id INTEGER NOT NULL,
            versio_pla INTEGER NOT NULL,
            canvi_id INTEGER NOT NULL,
            treballador_id TEXT NOT NULL,
            event_id TEXT NOT NULL UNIQUE,
            tipus TEXT NOT NULL,
            variables_json TEXT NOT NULL,
            estat TEXT NOT NULL DEFAULT 'pendent'
                CHECK (estat IN (
                    'pendent', 'previsualitzat', 'sense_mapping',
                    'enviat', 'duplicat', 'error'
                )),
            intents INTEGER NOT NULL DEFAULT 0,
            resposta_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sent_at TEXT,
            FOREIGN KEY (publicacio_id)
                REFERENCES publicacions_planificacio_cp_sat(id),
            FOREIGN KEY (versio_pla)
                REFERENCES versions_pla_publicat(versio),
            FOREIGN KEY (canvi_id)
                REFERENCES canvis_planificacio_cp_sat(id),
            CHECK (intents >= 0)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_notificacions_xivato_publicacio
        ON notificacions_xivato(publicacio_id, estat, id)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS tancaments_hores_realitzades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_inici TEXT NOT NULL,
            data_fi TEXT NOT NULL,
            estat TEXT NOT NULL DEFAULT 'tancat'
                CHECK (estat IN ('tancat', 'revertit')),
            assignacions_confirmades INTEGER NOT NULL,
            minuts_confirmats INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reverted_at TEXT,
            motiu_reversio TEXT,
            CHECK (data_fi >= data_inici),
            CHECK (assignacions_confirmades >= 0),
            CHECK (minuts_confirmats >= 0)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS hores_realitzades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tancament_id INTEGER NOT NULL,
            assignacio_id INTEGER NOT NULL,
            treballador_id TEXT NOT NULL,
            data TEXT NOT NULL,
            servei TEXT NOT NULL,
            hora_inici TEXT NOT NULL,
            hora_fi TEXT NOT NULL,
            durada_minuts INTEGER NOT NULL,
            es_canvi_zona INTEGER NOT NULL DEFAULT 0,
            es_canvi_torn INTEGER NOT NULL DEFAULT 0,
            estat TEXT NOT NULL DEFAULT 'confirmada'
                CHECK (estat IN ('confirmada', 'anul_lada')),
            origen TEXT NOT NULL DEFAULT 'pla_publicat',
            observacions TEXT,
            confirmada_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            anul_lada_at TEXT,
            FOREIGN KEY (tancament_id)
                REFERENCES tancaments_hores_realitzades(id),
            CHECK (durada_minuts > 0)
        )
        """
    )
    publication_columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(publicacions_planificacio_cp_sat)"
        )
    }
    for column in ("affected_anterior_hash", "affected_posterior_hash"):
        if column not in publication_columns:
            connection.execute(
                f"ALTER TABLE publicacions_planificacio_cp_sat "
                f"ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
            )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_publicacions_planificacio_execucio
        ON publicacions_planificacio_cp_sat(execucio_id, id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_execucions_planificacio_estat_periode
        ON execucions_planificacio_cp_sat(estat, data_inici, data_fi)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_canvis_planificacio_execucio
        ON canvis_planificacio_cp_sat(execucio_id, ordre)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bloquejos_planificacio_vigents
        ON bloquejos_planificacio(necessitat_id, estat, vigent_des_de, vigent_fins)
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_preassignacions_necessitat_activa
        ON preassignacions_planificacio(necessitat_id)
        WHERE estat = 'activa'
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_preassignacions_periode
        ON preassignacions_planificacio(estat, data, servei)
        """
    )
    connection.execute("DROP INDEX IF EXISTS idx_versions_pla_publicat_event")
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_versions_pla_publicat_event
        ON versions_pla_publicat(tipus_event, publicacio_id)
        WHERE publicacio_id IS NOT NULL
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_versions_pla_publicat_bootstrap
        ON versions_pla_publicat(tipus_event)
        WHERE tipus_event = 'bootstrap'
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_versions_pla_publicat_data
        ON versions_pla_publicat(created_at, versio)
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_hores_realitzades_necessitat_activa
        ON hores_realitzades(data, servei)
        WHERE estat = 'confirmada'
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_hores_realitzades_treballador_data
        ON hores_realitzades(treballador_id, data, estat)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_hores_realitzades_tancament
        ON hores_realitzades(tancament_id, estat)
        """
    )
    if _table_exists(connection, "assig_grup_T"):
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_assig_grup_t_estat_data
            ON assig_grup_T(estat_planificacio, data)
            """
        )
    _bootstrap_published_plan_version(connection)
    connection.executemany(
        """
        INSERT OR IGNORE INTO migracions_planificacio_cp_sat (versio)
        VALUES (?)
        """,
        ((version,) for version in range(1, SCHEMA_VERSION + 1)),
    )


def migrate_planning_schema(database_path: str | Path) -> None:
    """Aplica l'esquema genèric i repara la clau forana antiga."""
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            _repair_legacy_replanning_foreign_key(connection)
            _migrate_service_night_indicator(connection)
            _migrate_partial_publications(connection)
            _migrate_invalidated_execution_state(connection)
            _create_generic_schema(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

        obsolete = [
            row
            for row in connection.execute("PRAGMA foreign_key_check")
            if row[2] == "incidencies_personal_v1"
        ]
        if obsolete:
            raise PlanningSchemaMigrationError(
                "La migració conserva referències a incidencies_personal_v1"
            )
