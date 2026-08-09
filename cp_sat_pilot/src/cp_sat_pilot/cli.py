from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict
from datetime import date
from pathlib import Path

from .sqlite_adapter import SqliteInputError, load_problem_from_sqlite
from .model import CpSatPlanner, SolverConfig
from .multistart import MultiStartSelectionError, solve_adaptive_multi_start


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "La data ha de tenir format AAAA-MM-DD"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    repository_root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(
        description="Pilot CP-SAT de Fase 1, sense publicació a SQLite"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=repository_root / "data" / "treballadors.db",
        help="Base SQLite d'entrada, oberta en mode de només lectura",
    )
    parser.add_argument("--start-date", type=_iso_date)
    parser.add_argument("--end-date", type=_iso_date)
    parser.add_argument(
        "--duplicate-policy",
        choices=("replace_all", "add_new_only"),
        default="replace_all",
    )
    parser.add_argument("--time-limit", type=float, default=60.0)
    parser.add_argument("--equity-time-limit", type=float, default=15.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--multi-seed",
        type=int,
        nargs="+",
        help=(
            "Llavors disponibles per al mode adaptatiu; normalment s'atura "
            "després de la primera si la cobertura completa queda provada"
        ),
    )
    parser.add_argument(
        "--all-seeds",
        action="store_true",
        help="Força totes les llavors per obtenir alternatives o fer diagnòstic",
    )
    parser.add_argument(
        "--json",
        type=Path,
        help="Fitxer JSON opcional. No s'escriu mai a SQLite.",
    )
    parser.add_argument("--solver-log", action="store_true")
    return parser


def _result_as_json(result) -> dict:
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        problem = load_problem_from_sqlite(
            args.db,
            start_date=args.start_date,
            end_date=args.end_date,
            duplicate_policy=args.duplicate_policy,
        )
    except (OSError, sqlite3.Error, SqliteInputError) as exc:
        print(f"Error d'entrada: {exc}")
        return 2

    planner = CpSatPlanner(problem)
    config = SolverConfig(
        max_time_seconds=args.time_limit,
        equity_time_seconds=args.equity_time_limit,
        num_workers=args.workers,
        random_seed=args.seed,
        log_search_progress=args.solver_log,
    )
    selection = None
    try:
        if args.multi_seed:
            selection = solve_adaptive_multi_start(
                planner,
                config,
                args.multi_seed,
                force_all_seeds=args.all_seeds,
            )
            result = selection.selected_result
        else:
            result = planner.solve(config)
    except MultiStartSelectionError as exc:
        print(f"Error multillavor: {exc}")
        return 1

    if selection:
        print("MODE MULTILLAVOR ADAPTATIU")
        if selection.stopped_after_first_seed:
            print(
                "Cobertura completa i òptima amb la primera llavor; "
                "no s'han executat les restants."
            )
        for candidate in selection.candidates:
            marker = "*" if candidate.seed == selection.selected_seed else "-"
            hours = (
                f"{candidate.annual_hours_range:.2f} h"
                if candidate.annual_hours_range is not None
                else "n/d"
            )
            zone = (
                f"{candidate.zone_rate_range_points:.1f}"
                if candidate.zone_rate_range_points is not None
                else "n/d"
            )
            turn = (
                f"{candidate.turn_rate_range_points:.1f}"
                if candidate.turn_rate_range_points is not None
                else "n/d"
            )
            print(
                f"{marker} llavor {candidate.seed}: "
                f"vàlida={candidate.feasible}; "
                f"cobertura={candidate.covered_needs}/{candidate.total_needs}; "
                f"hores={hours}; zona={zone}; torn={turn}; "
                f"pla={candidate.assignment_fingerprint}"
            )
        print(f"Llavor seleccionada: {selection.selected_seed}")
        print(
            "Temps acumulat multillavor: "
            f"{selection.total_wall_time_seconds:.3f} s"
        )

    print("PILOT CP-SAT — FASE 2")
    print(f"Estat: {result.status}")
    print(f"Cobertura: {result.covered_needs}/{result.total_needs}")
    print(f"Variables candidates: {result.candidate_variables}")
    print(
        "Restriccions d'incompatibilitat 12 h/solapament: "
        f"{result.incompatibility_constraints}"
    )
    print(f"Temps: {result.wall_time_seconds:.3f} s")
    if result.relative_gap is not None:
        print(f"Gap de cobertura: {result.relative_gap:.6f}")
    if result.optimization_phases:
        print("Fases d'optimització:")
        for phase in result.optimization_phases:
            objective = (
                f"{phase.objective_value:.0f}"
                if phase.objective_value is not None
                else "n/d"
            )
            bound = (
                f"{phase.best_objective_bound:.0f}"
                if phase.best_objective_bound is not None
                else "n/d"
            )
            gap = (
                f"{phase.relative_gap:.3%}"
                if phase.relative_gap is not None
                else "n/d"
            )
            print(
                f"- {phase.name}: {phase.status}; "
                f"objectiu={objective}; cota={bound}; gap={gap}; "
                f"temps={phase.wall_time_seconds:.3f} s"
            )
    if result.soft_metrics:
        metrics = result.soft_metrics
        print("Objectius tous:")
        print(
            "- Alteracions del pla de referència: "
            f"{metrics.plan_alterations}"
        )
        print(
            "- Preferències de substitució no satisfetes: "
            f"{metrics.preferred_assignment_violations}"
        )
        print(
            "- Excés de finestres de 10 dies: "
            f"{metrics.consecutive_excess_windows}"
        )
        print(f"- Violació regla de divendres: {metrics.friday_violation}")
        print(f"- Canvis de zona: {metrics.zone_changes}")
        print(f"- Canvis de torn: {metrics.turn_changes}")
        print(
            "- Diferència anual màxima d'hores: "
            f"{metrics.annual_hours_range_minutes / 60:.2f} h"
        )
        print(
            "- Penalització d'equitat d'hores anuals: "
            f"{metrics.annual_hours_equity_penalty}"
        )
        print(
            "- Penalització d'equitat acumulada de zona: "
            f"{metrics.accumulated_zone_equity_penalty}"
        )
        print(
            "- Diferència màxima de taxa acumulada de zona: "
            f"{metrics.accumulated_zone_rate_range_permille / 10:.1f} punts percentuals"
        )
        print(
            "- Penalització d'equitat acumulada de torn: "
            f"{metrics.accumulated_turn_equity_penalty}"
        )
        print(
            "- Diferència màxima de taxa acumulada de torn: "
            f"{metrics.accumulated_turn_rate_range_permille / 10:.1f} punts percentuals"
        )
        print(
            "- Pitjor diferència relativa entre taxes de canvi: "
            f"{metrics.worst_change_equity_gap_permille / 10:.1f} %"
        )
        print(
            "- Objectiu combinat d'equitat oportunista: "
            f"{metrics.opportunistic_equity_objective}"
        )
    if result.validation_errors:
        print("Errors del validador final:")
        for error in result.validation_errors:
            print(f"- {error}")
    if args.json:
        payload = _result_as_json(result)
        if selection:
            payload["multi_start"] = {
                "selected_seed": selection.selected_seed,
                "total_wall_time_seconds": selection.total_wall_time_seconds,
                "candidates": [
                    asdict(candidate) for candidate in selection.candidates
                ],
            }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Resultat JSON: {args.json.resolve()}")

    return 0 if result.feasible else 1


if __name__ == "__main__":
    raise SystemExit(main())
