from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .sqlite_adapter import SqliteInputError, load_problem_from_sqlite
from .model import CpSatPlanner, SolverConfig
from .stability import (
    StabilityRun,
    aggregate_stability_runs,
    summarize_stability_run,
)


def build_parser() -> argparse.ArgumentParser:
    repository_root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(
        description="Matriu d'estabilitat del punt 8 del pilot CP-SAT"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=repository_root / "data" / "treballadors.db",
        help="Base SQLite d'entrada, oberta en mode de només lectura",
    )
    parser.add_argument(
        "--time-limit",
        type=float,
        nargs="+",
        default=[5.0, 15.0],
        help="Límits per a cobertura, estabilitat i preferències",
    )
    parser.add_argument(
        "--equity-time-limit",
        type=float,
        default=15.0,
        help="Límit fix de la fase oportunista d'equitat",
    )
    parser.add_argument(
        "--num-workers",
        type=int,
        nargs="+",
        default=[1, 8],
        help="Un o més nombres de workers del solver",
    )
    parser.add_argument(
        "--seed",
        type=int,
        nargs="+",
        default=[0, 1, 2],
        help="Una o més llavors",
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument(
        "--json",
        type=Path,
        help="Fitxer de resultats opcional. No s'escriu mai a SQLite.",
    )
    return parser


def _optional(value: float | int | None, decimals: int = 1) -> str:
    if value is None:
        return "n/d"
    return f"{value:.{decimals}f}"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if any(value <= 0 for value in args.time_limit):
        print("Error d'entrada: els límits de temps han de ser positius")
        return 2
    if args.equity_time_limit <= 0:
        print("Error d'entrada: el límit d'equitat ha de ser positiu")
        return 2
    if any(value <= 0 for value in args.num_workers):
        print("Error d'entrada: els nombres de workers han de ser positius")
        return 2
    if args.repetitions <= 0:
        print("Error d'entrada: les repeticions han de ser positives")
        return 2

    try:
        problem = load_problem_from_sqlite(
            args.db,
            duplicate_policy="replace_all",
        )
    except (OSError, sqlite3.Error, SqliteInputError) as exc:
        print(f"Error d'entrada: {exc}")
        return 2

    runs: list[StabilityRun] = []
    total_runs = (
        len(set(args.time_limit))
        * len(set(args.num_workers))
        * len(set(args.seed))
        * args.repetitions
    )
    print(f"ESTABILITAT PUNT 8 — {total_runs} execucions")
    current = 0
    for time_limit in sorted(set(args.time_limit)):
        for num_workers in sorted(set(args.num_workers)):
            for seed in sorted(set(args.seed)):
                for repetition in range(1, args.repetitions + 1):
                    current += 1
                    result = CpSatPlanner(problem).solve(
                        SolverConfig(
                            max_time_seconds=time_limit,
                            equity_time_seconds=args.equity_time_limit,
                            num_workers=num_workers,
                            random_seed=seed,
                        )
                    )
                    run = summarize_stability_run(
                        result,
                        time_limit_seconds=time_limit,
                        num_workers=num_workers,
                        seed=seed,
                        repetition=repetition,
                    )
                    runs.append(run)
                    print(
                        f"[{current}/{total_runs}] {time_limit:g}s "
                        f"w={num_workers} seed={seed} rep={repetition}: "
                        f"cov={run.covered}/{run.total_needs}; "
                        f"hores={_optional(run.annual_hours_range)}; "
                        f"zona={_optional(run.zone_rate_range_points)}; "
                        f"torn={_optional(run.turn_rate_range_points)}; "
                        f"pla={run.assignment_fingerprint}; "
                        f"temps={run.wall_time_seconds:.1f}s"
                    )

    aggregates = aggregate_stability_runs(runs)
    print("\nRESUM PER TEMPS I WORKERS")
    for item in aggregates:
        print(
            f"- {item.time_limit_seconds:g}s / {item.num_workers} workers: "
            f"cobertura={item.coverage_min}-{item.coverage_max}; "
            f"hores={_optional(item.annual_hours_min)}-"
            f"{_optional(item.annual_hours_max)} "
            f"(σ={_optional(item.annual_hours_stddev)}); "
            f"zona={_optional(item.zone_rate_min)}-"
            f"{_optional(item.zone_rate_max)}; "
            f"torn={_optional(item.turn_rate_min)}-"
            f"{_optional(item.turn_rate_max)}; "
            f"fases estabilitat/equitat="
            f"{item.stability_solved_runs}/{item.equity_solved_runs} "
            f"de {item.runs}; "
            f"plans={item.unique_assignment_plans}/{item.runs}"
        )

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "runs": [asdict(run) for run in runs],
                    "aggregates": [asdict(item) for item in aggregates],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nResultats JSON: {args.json.resolve()}")

    return 0 if all(run.validation_errors == 0 for run in runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
