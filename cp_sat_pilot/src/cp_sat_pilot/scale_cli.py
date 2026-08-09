from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .sqlite_adapter import SqliteInputError, load_problem_from_sqlite
from .model import CpSatPlanner, SolverConfig
from .multistart import MultiStartSelectionError, solve_multi_start
from .scale import build_scaled_problem, peak_working_set_bytes


def build_parser() -> argparse.ArgumentParser:
    repository_root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(
        description="Prova d'escala sintètica del punt 9"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=repository_root / "data" / "treballadors.db",
        help="Base SQLite d'entrada, oberta en mode de només lectura",
    )
    parser.add_argument("--copies", type=int, default=3)
    parser.add_argument("--time-limit", type=float, default=60.0)
    parser.add_argument("--equity-time-limit", type=float, default=15.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, nargs="+", default=[0, 1, 2])
    parser.add_argument(
        "--json",
        type=Path,
        help="Fitxer de resultats opcional. No s'escriu mai a SQLite.",
    )
    return parser


def _result_payload(result) -> dict:
    payload = asdict(result)
    payload["assignments"] = [
        {
            **asdict(assignment),
            "date": assignment.date.isoformat(),
            "start": assignment.start.isoformat(),
            "end": assignment.end.isoformat(),
        }
        for assignment in result.assignments
    ]
    return payload


def _optional(value: float | None, suffix: str = "") -> str:
    return "n/d" if value is None else f"{value:.1f}{suffix}"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        base_problem = load_problem_from_sqlite(
            args.db,
            duplicate_policy="replace_all",
        )
        problem = build_scaled_problem(base_problem, copies=args.copies)
        selection = solve_multi_start(
            CpSatPlanner(problem),
            SolverConfig(
                max_time_seconds=args.time_limit,
                equity_time_seconds=args.equity_time_limit,
                num_workers=args.workers,
                random_seed=args.seed[0],
            ),
            args.seed,
        )
    except (
        OSError,
        sqlite3.Error,
        SqliteInputError,
        MultiStartSelectionError,
        ValueError,
    ) as exc:
        print(f"Error d'escala: {exc}")
        return 2

    result = selection.selected_result
    memory_bytes = peak_working_set_bytes()
    print("PROVA D'ESCALA — PUNT 9")
    print(
        f"Necessitats: {len(base_problem.needs)} × {args.copies} "
        f"= {len(problem.needs)}"
    )
    print(
        f"Període sintètic: {min(need.date for need in problem.needs)} — "
        f"{max(need.date for need in problem.needs)}"
    )
    print(
        f"Configuració: {args.time_limit:g} s per fase, "
        f"{args.equity_time_limit:g} s per a equitat, "
        f"{args.workers} workers, llavors={list(dict.fromkeys(args.seed))}"
    )
    for candidate in selection.candidates:
        marker = "*" if candidate.seed == selection.selected_seed else "-"
        print(
            f"{marker} llavor {candidate.seed}: "
            f"cobertura={candidate.covered_needs}/{candidate.total_needs}; "
            f"fase={candidate.coverage_phase_status}; "
            f"gap={_optional(candidate.coverage_phase_gap * 100 if candidate.coverage_phase_gap is not None else None, ' %')}; "
            f"hores={_optional(candidate.annual_hours_range, ' h')}; "
            f"zona={_optional(candidate.zone_rate_range_points)}; "
            f"torn={_optional(candidate.turn_rate_range_points)}; "
            f"gap equitat={_optional(candidate.equity_phase_gap * 100 if candidate.equity_phase_gap is not None else None, ' %')}; "
            f"temps={candidate.wall_time_seconds:.1f} s"
        )
    print(f"Llavor seleccionada: {selection.selected_seed}")
    print(
        f"Temps acumulat: {selection.total_wall_time_seconds:.1f} s; "
        f"pic de memòria: "
        f"{_optional(memory_bytes / 1024**2 if memory_bytes else None, ' MiB')}"
    )
    print(
        f"Model seleccionat: candidats={result.candidate_variables}; "
        f"incompatibilitats={result.incompatibility_constraints}; "
        f"errors={len(result.validation_errors)}"
    )

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "scale": {
                        "copies": args.copies,
                        "base_needs": len(base_problem.needs),
                        "scaled_needs": len(problem.needs),
                        "start_date": min(
                            need.date for need in problem.needs
                        ).isoformat(),
                        "end_date": max(
                            need.date for need in problem.needs
                        ).isoformat(),
                        "time_limit_seconds": args.time_limit,
                        "equity_time_limit_seconds": args.equity_time_limit,
                        "num_workers": args.workers,
                        "seeds": list(dict.fromkeys(args.seed)),
                        "peak_working_set_bytes": memory_bytes,
                    },
                    "multi_start": {
                        "selected_seed": selection.selected_seed,
                        "total_wall_time_seconds": (
                            selection.total_wall_time_seconds
                        ),
                        "candidates": [
                            asdict(candidate)
                            for candidate in selection.candidates
                        ],
                    },
                    "selected_result": _result_payload(result),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Resultat JSON: {args.json.resolve()}")

    return 0 if result.feasible else 1


if __name__ == "__main__":
    raise SystemExit(main())
