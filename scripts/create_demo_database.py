"""Genera una base SQLite de demostració sense conservar identitats reals.

La transformació és determinista per facilitar proves repetibles, però no
desa cap taula de correspondències. La base d'origen s'obre en mode de només
lectura i el resultat només substitueix el fitxer de sortida quan totes les
validacions han acabat correctament.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import sqlite3
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote


DEFAULT_SEED = 20260801
DATE_SHIFT_DAYS = 728  # 104 setmanes: conserva el dia de la setmana.

CLEAR_TABLES = (
    "ajustos_descans_substitucio",
    "assig_grup_A",
    "auditoria_planificacio",
    "descansos_dies_old",
    "historic_assignacions_old",
    "incidencies_personal",
    "proposta_canvis",
    "proposta_inicial_cp_sat_elements",
    "propostes_inicials_cp_sat",
    "propostes_replanificacio",
    "publicacions_inicials_cp_sat",
)

WORKER_COLUMNS = (
    "id",
    "treballador",
    "plaza",
    "rotacio",
    "zona",
    "habilitacions",
    "línia",
    "categoria",
    "grup",
    "denominació",
)

CATALAN_MONTHS = (
    "Gener",
    "Febrer",
    "Març",
    "Abril",
    "Maig",
    "Juny",
    "Juliol",
    "Agost",
    "Setembre",
    "Octubre",
    "Novembre",
    "Desembre",
)


def _rng(seed: int, label: str) -> random.Random:
    digest = hashlib.sha256(f"{seed}:{label}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _source_uri(path: Path) -> str:
    return f"file:{quote(path.resolve().as_posix(), safe='/:')}?mode=ro"


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _shift_text(value: object, days: int = DATE_SHIFT_DAYS) -> object:
    if value is None or not isinstance(value, str) or not value.strip():
        return value

    formats = (
        ("%Y-%m-%d", "%Y-%m-%d"),
        ("%d/%m/%Y", "%d/%m/%Y"),
        ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"),
        ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S"),
    )
    for parser, formatter in formats:
        try:
            parsed = datetime.strptime(value, parser)
        except ValueError:
            continue
        return (parsed + timedelta(days=days)).strftime(formatter)
    return value


def _grouped_permutation_map(
    old_ids_by_group: dict[str, list[int]],
    synthetic_ids_by_group: dict[str, list[int]],
    *,
    seed: int,
    label: str,
) -> dict[int, int]:
    result: dict[int, int] = {}
    for group in sorted(old_ids_by_group):
        old_ids = list(old_ids_by_group[group])
        new_ids = list(synthetic_ids_by_group[group])
        _rng(seed, f"{label}:{group}").shuffle(new_ids)
        result.update(zip(old_ids, new_ids, strict=True))
    return result


def _map_worker_id(value: object, mapping: dict[int, int]) -> object:
    if value is None or str(value).strip() == "":
        return value
    try:
        old_id = int(str(value).strip())
    except ValueError as exc:
        raise ValueError("S'ha trobat un identificador de treballador no numèric") from exc
    if old_id not in mapping:
        raise ValueError("S'ha trobat un identificador de treballador orfe")
    return mapping[old_id]


def _replace_workers(
    connection: sqlite3.Connection, *, seed: int
) -> tuple[
    dict[int, int],
    dict[int, dict[str, object]],
    list[str],
    list[str],
]:
    connection.row_factory = sqlite3.Row
    original = [
        dict(row)
        for row in connection.execute(
            f"SELECT {', '.join(WORKER_COLUMNS)} FROM treballadors ORDER BY id"
        )
    ]
    if not original:
        raise ValueError("La taula treballadors és buida")

    original_names = [str(row["treballador"]) for row in original if row["treballador"]]
    original_places = [str(row["plaza"]) for row in original if row["plaza"]]

    by_group: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in original:
        by_group[str(row["grup"] or "")].append(row)

    old_ids_by_group: dict[str, list[int]] = {}
    synthetic_ids_by_group: dict[str, list[int]] = {}
    next_id = 10001
    for group in sorted(by_group):
        rows = sorted(by_group[group], key=lambda item: int(item["id"]))
        old_ids_by_group[group] = [int(row["id"]) for row in rows]
        new_ids = list(range(next_id, next_id + len(rows)))
        synthetic_ids_by_group[group] = new_ids
        next_id += len(rows)

    identity_map = _grouped_permutation_map(
        old_ids_by_group,
        synthetic_ids_by_group,
        seed=seed,
        label="entity",
    )

    profiles: list[dict[str, object]] = []
    for row in original:
        new_id = identity_map[int(row["id"])]
        profiles.append(
            {
                "id": new_id,
                "treballador": f"Persona D{new_id - 10000:03d}",
                # És una clau operativa compartida amb serveis.opcio_1/opcio_2.
                # Conservar-la és necessari per reconstruir cobertura i no
                # afegeix cap valor que no sigui ja present a la taula serveis.
                "plaza": row["plaza"],
                "rotacio": row["rotacio"],
                "zona": row["zona"],
                "habilitacions": row["habilitacions"],
                "línia": row["línia"],
                "categoria": row["categoria"],
                "grup": row["grup"],
                "denominació": row["denominació"],
            }
        )
    profiles.sort(key=lambda profile: int(profile["id"]))

    connection.execute("DELETE FROM treballadors")
    placeholders = ", ".join("?" for _ in WORKER_COLUMNS)
    connection.executemany(
        f"INSERT INTO treballadors ({', '.join(WORKER_COLUMNS)}) VALUES ({placeholders})",
        [tuple(profile[column] for column in WORKER_COLUMNS) for profile in profiles],
    )
    profile_by_id = {int(profile["id"]): profile for profile in profiles}
    return (
        identity_map,
        profile_by_id,
        original_names,
        original_places,
    )


def _transform_rests(
    connection: sqlite3.Connection, mapping: dict[int, int]
) -> None:
    rows = connection.execute(
        "SELECT rowid, treballador_id, treballador_substitut_id, data, motiu "
        "FROM descansos_dies"
    ).fetchall()
    for rowid, worker_id, substitute_id, value_date, reason in rows:
        new_worker_id = _map_worker_id(worker_id, mapping)
        new_substitute_id = _map_worker_id(substitute_id, mapping)
        shifted = _shift_text(value_date)
        generic_reason = "Dada de demostració" if reason else None
        connection.execute(
            "UPDATE descansos_dies SET treballador_id=?, treballador_substitut_id=?, "
            "data=?, motiu=? WHERE rowid=?",
            (new_worker_id, new_substitute_id, shifted, generic_reason, rowid),
        )


def _transform_history(
    connection: sqlite3.Connection, mapping: dict[int, int]
) -> None:
    rows = connection.execute(
        "SELECT rowid, treballador_id, data, data_apunt FROM historic_assignacions"
    ).fetchall()
    for rowid, worker_id, value_date, noted_at in rows:
        connection.execute(
            "UPDATE historic_assignacions SET treballador_id=?, data=?, data_apunt=? "
            "WHERE rowid=?",
            (
                _map_worker_id(worker_id, mapping),
                _shift_text(value_date),
                _shift_text(noted_at),
                rowid,
            ),
        )


def _transform_published_plan(
    connection: sqlite3.Connection,
    mapping: dict[int, int],
    profiles: dict[int, dict[str, object]],
) -> None:
    rows = connection.execute(
        "SELECT rowid, treballador_id, data, created_at FROM assig_grup_T"
    ).fetchall()
    for rowid, worker_id, value_date, created_at in rows:
        new_id = int(_map_worker_id(worker_id, mapping))
        profile = profiles[new_id]
        connection.execute(
            "UPDATE assig_grup_T SET treballador_id=?, treballador_nom=?, "
            "treballador_plaza=?, treballador_grup=?, data=?, created_at=? WHERE rowid=?",
            (
                str(new_id),
                profile["treballador"],
                profile["plaza"],
                profile["grup"],
                _shift_text(value_date),
                _shift_text(created_at),
                rowid,
            ),
        )

    hours = {
        int(worker_id): float(total or 0.0)
        for worker_id, total in connection.execute(
            "SELECT treballador_id, SUM(durada_hores) FROM historic_assignacions "
            "GROUP BY treballador_id"
        )
    }
    for worker_id, total in hours.items():
        connection.execute(
            "UPDATE assig_grup_T SET hores_totals_any=? WHERE treballador_id=?",
            (total, str(worker_id)),
        )


def _transform_coverage(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT rowid, data, motiu_no_cobert FROM cobertura"
    ).fetchall()
    for rowid, value_date, uncovered_reason in rows:
        connection.execute(
            "UPDATE cobertura SET data=?, motiu_no_cobert=? WHERE rowid=?",
            (
                _shift_text(value_date),
                "Necessitat de demostració" if uncovered_reason else None,
                rowid,
            ),
        )


def _transform_calendar(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        'SELECT rowid, Data FROM serveis_calendari'
    ).fetchall()
    for rowid, value_date in rows:
        shifted = str(_shift_text(value_date))
        parsed = datetime.strptime(shifted, "%d/%m/%Y").date()
        connection.execute(
            'UPDATE serveis_calendari SET Data=?, Dia_Mes=?, Dia_Num=? WHERE rowid=?',
            (
                shifted,
                f"{parsed.day} {CATALAN_MONTHS[parsed.month - 1]}",
                f"D{parsed.timetuple().tm_yday:03d}",
                rowid,
            ),
        )


def _clear_operational_state(connection: sqlite3.Connection) -> None:
    for table in CLEAR_TABLES:
        if _table_exists(connection, table):
            connection.execute(f'DELETE FROM "{table}"')
    if _table_exists(connection, "sqlite_sequence"):
        placeholders = ", ".join("?" for _ in CLEAR_TABLES)
        connection.execute(
            f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})", CLEAR_TABLES
        )


def _text_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
        if "TEXT" in str(row[2]).upper() or not str(row[2]).strip()
    ]


def _count_sensitive_text_hits(
    connection: sqlite3.Connection,
    sensitive_values: list[str],
    *,
    selected_columns: set[tuple[str, str]] | None = None,
) -> int:
    needles = {value.casefold() for value in sensitive_values if len(value.strip()) >= 4}
    hits = 0
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for table in tables:
        for column in _text_columns(connection, table):
            if selected_columns is not None and (table, column) not in selected_columns:
                continue
            for (value,) in connection.execute(
                f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
            ):
                text = str(value).casefold()
                hits += sum(needle in text for needle in needles)
    return hits


def _validate(
    connection: sqlite3.Connection,
    *,
    original_names: list[str],
    original_places: list[str],
) -> dict[str, object]:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise ValueError(f"SQLite integrity_check: {integrity}")

    workers = connection.execute(
        "SELECT id, treballador, plaza, grup FROM treballadors ORDER BY id"
    ).fetchall()
    if not workers or any(
        int(worker_id) < 10001
        or not str(name).startswith("Persona D")
        or not str(place).strip()
        for worker_id, name, place, _group in workers
    ):
        raise ValueError("Les identitats sintètiques no compleixen el patró esperat")

    uncleared = {
        table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in CLEAR_TABLES
        if _table_exists(connection, table)
        and connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    }
    if uncleared:
        raise ValueError("No s'han buidat totes les taules operatives")

    name_hits = _count_sensitive_text_hits(connection, original_names)
    if name_hits:
        raise ValueError(
            "La comprovació de privacitat ha detectat noms originals"
        )

    group_counts = dict(
        connection.execute(
            "SELECT grup, COUNT(*) FROM treballadors GROUP BY grup ORDER BY grup"
        ).fetchall()
    )
    return {
        "integrity": integrity,
        "workers": len(workers),
        "group_counts": group_counts,
        "original_name_hits": name_hits,
        "operational_place_codes_retained": len(set(original_places)),
        "cleared_tables": len(CLEAR_TABLES),
    }


def create_demo_database(
    source: Path, output: Path, *, seed: int = DEFAULT_SEED, replace: bool = False
) -> dict[str, object]:
    source = source.resolve()
    output = output.resolve()
    if source == output:
        raise ValueError("L'origen i la sortida no poden ser el mateix fitxer")
    if not source.is_file():
        raise FileNotFoundError(f"No existeix la base d'origen: {source}")
    if output.exists() and not replace:
        raise FileExistsError(
            f"La sortida ja existeix: {output}. Useu --replace per regenerar-la."
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.stem}-", suffix=".tmp.db", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        source_connection = sqlite3.connect(_source_uri(source), uri=True)
        destination = sqlite3.connect(temporary)
        try:
            source_connection.backup(destination)
        finally:
            source_connection.close()
        try:
            destination.execute("PRAGMA journal_mode=DELETE")
            destination.execute("PRAGMA secure_delete=ON")
            destination.execute("PRAGMA foreign_keys=OFF")
            destination.execute("BEGIN IMMEDIATE")
            try:
                (
                    identity_map,
                    profiles,
                    original_names,
                    original_places,
                ) = _replace_workers(destination, seed=seed)
                _transform_rests(destination, identity_map)
                _transform_history(destination, identity_map)
                _transform_published_plan(destination, identity_map, profiles)
                _transform_coverage(destination)
                _transform_calendar(destination)
                _clear_operational_state(destination)
                destination.commit()
            except Exception:
                destination.rollback()
                raise

            destination.execute("VACUUM")
            report = _validate(
                destination,
                original_names=original_names,
                original_places=original_places,
            )
        finally:
            destination.close()

        raw = temporary.read_bytes()
        raw_name_hits = sum(
            name.encode("utf-8") in raw
            for name in original_names
            if len(name.strip()) >= 4
        )
        if raw_name_hits:
            raise ValueError("S'han detectat noms originals als bytes del fitxer final")
        report["raw_original_name_hits"] = raw_name_hits
        report["seed"] = seed
        report["date_shift_days"] = DATE_SHIFT_DAYS
        os.replace(temporary, output)
        return report
    finally:
        if temporary.exists():
            temporary.unlink()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crea una base pseudonimitzada de demostració per al prototip CP-SAT."
    )
    parser.add_argument("--source", type=Path, required=True, help="Base SQLite original")
    parser.add_argument("--output", type=Path, required=True, help="Base demo de sortida")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--replace", action="store_true", help="Substitueix una base demo ja existent"
    )
    return parser.parse_args()


def main() -> int:
    arguments = _parse_args()
    report = create_demo_database(
        arguments.source,
        arguments.output,
        seed=arguments.seed,
        replace=arguments.replace,
    )
    print("Base demo creada correctament")
    print(f"Treballadors sintètics: {report['workers']}")
    print(f"Distribució per grup: {report['group_counts']}")
    print(f"Taules operatives buidades: {report['cleared_tables']}")
    print("Noms originals detectats: 0")
    print("Codis de plaça operatius: conservats per mantenir la cobertura")
    print("Integritat SQLite: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
