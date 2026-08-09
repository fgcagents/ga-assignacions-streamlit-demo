from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from .sqlite_adapter import SqliteInputError, load_problem_from_sqlite
from .model import CpSatPlanner, SolverConfig
from .scenarios import ScenarioSpec, apply_scenario, build_standard_scenarios


@dataclass(frozen=True, slots=True)
class ScenarioSummary:
    name: str
    description: str
    start_date: str
    end_date: str
    workers_t: int
    absent_workers: tuple[str, ...]
    excluded_workers: tuple[str, ...]
    needs: int
    covered: int
    coverage_percent: float
    status: str
    coverage_phase_status: str
    coverage_gap: float | None
    validation_errors: int
    candidate_variables: int
    uncovered_need_ids: tuple[str, ...]
    uncovered_without_candidate: int
    uncovered_with_candidates: int
    uncovered_by_skills: tuple[tuple[str, int], ...]
    annual_hours_range: float | None
    zone_rate_range_points: float | None
    turn_rate_range_points: float | None
    zone_changes: int | None
    turn_changes: int | None
    wall_time_seconds: float


def build_parser() -> argparse.ArgumentParser:
    repository_root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(
        description="Bateria d'escenaris del punt 7 del pilot CP-SAT"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=repository_root / "data" / "treballadors.db",
        help="Base SQLite d'entrada, oberta en mode de només lectura",
    )
    parser.add_argument("--time-limit", type=float, default=5.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--scenario",
        action="append",
        help="Nom d'escenari a executar; es pot repetir",
    )
    parser.add_argument(
        "--json",
        type=Path,
        help="Fitxer resum opcional. No s'escriu mai a SQLite.",
    )
    return parser


def summarize(spec: ScenarioSpec, problem, result) -> ScenarioSummary:
    metrics = result.soft_metrics
    covered_need_ids = {assignment.need_id for assignment in result.assignments}
    uncovered_needs = tuple(
        need for need in problem.needs if need.id not in covered_need_ids
    )
    candidate_need_ids = {
        need_id for _, need_id in CpSatPlanner(problem).candidate_pairs()
    }
    coverage_phase = (
        result.optimization_phases[0] if result.optimization_phases else None
    )
    uncovered_skill_counts = Counter(
        "+".join(sorted(need.required_skills)) or "sense_habilitacio"
        for need in uncovered_needs
    )
    return ScenarioSummary(
        name=spec.name,
        description=spec.description,
        start_date=spec.start_date.isoformat(),
        end_date=spec.end_date.isoformat(),
        workers_t=sum(worker.group == "T" for worker in problem.workers),
        absent_workers=tuple(sorted(spec.absent_worker_ids)),
        excluded_workers=tuple(sorted(spec.excluded_worker_ids)),
        needs=result.total_needs,
        covered=result.covered_needs,
        coverage_percent=(
            100.0 * result.covered_needs / result.total_needs
            if result.total_needs
            else 100.0
        ),
        status=result.status,
        coverage_phase_status=(
            coverage_phase.status if coverage_phase else "NO_EXECUTADA"
        ),
        coverage_gap=coverage_phase.relative_gap if coverage_phase else None,
        validation_errors=len(result.validation_errors),
        candidate_variables=result.candidate_variables,
        uncovered_need_ids=tuple(need.id for need in uncovered_needs),
        uncovered_without_candidate=sum(
            need.id not in candidate_need_ids for need in uncovered_needs
        ),
        uncovered_with_candidates=sum(
            need.id in candidate_need_ids for need in uncovered_needs
        ),
        uncovered_by_skills=tuple(sorted(uncovered_skill_counts.items())),
        annual_hours_range=(
            metrics.annual_hours_range_minutes / 60 if metrics else None
        ),
        zone_rate_range_points=(
            metrics.accumulated_zone_rate_range_permille / 10
            if metrics
            else None
        ),
        turn_rate_range_points=(
            metrics.accumulated_turn_rate_range_permille / 10
            if metrics
            else None
        ),
        zone_changes=metrics.zone_changes if metrics else None,
        turn_changes=metrics.turn_changes if metrics else None,
        wall_time_seconds=result.wall_time_seconds,
    )


def run_scenario(
    database_path: Path,
    spec: ScenarioSpec,
    config: SolverConfig,
) -> ScenarioSummary:
    period_problem = load_problem_from_sqlite(
        database_path,
        start_date=spec.start_date,
        end_date=spec.end_date,
        duplicate_policy="replace_all",
    )
    scenario_problem = apply_scenario(period_problem, spec)
    result = CpSatPlanner(scenario_problem).solve(config)
    return summarize(spec, scenario_problem, result)


def _format_optional(value: float | int | None, suffix: str = "") -> str:
    return "n/d" if value is None else f"{value:.1f}{suffix}"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        base_problem = load_problem_from_sqlite(
            args.db,
            duplicate_policy="replace_all",
        )
        scenarios = build_standard_scenarios(base_problem)
        if args.scenario:
            selected_names = set(args.scenario)
            known_names = {scenario.name for scenario in scenarios}
            unknown_names = selected_names.difference(known_names)
            if unknown_names:
                raise SqliteInputError(
                    "Escenaris inexistents: " + ", ".join(sorted(unknown_names))
                )
            scenarios = tuple(
                scenario
                for scenario in scenarios
                if scenario.name in selected_names
            )

        config = SolverConfig(
            max_time_seconds=args.time_limit,
            num_workers=args.workers,
            random_seed=args.seed,
        )
        summaries = tuple(
            run_scenario(args.db, scenario, config)
            for scenario in scenarios
        )
    except (OSError, sqlite3.Error, SqliteInputError, ValueError) as exc:
        print(f"Error d'entrada: {exc}")
        return 2

    print("VALIDACIÓ D'ESCENARIS — PUNT 7")
    print(
        f"Configuració: {args.time_limit:.1f} s per fase, "
        f"{args.workers} workers, llavor {args.seed}"
    )
    if args.time_limit < 60:
        print(
            "Avís: amb menys de 60 s, les mètriques d'equitat són "
            "diagnòstiques; la cobertura conserva la seva prova pròpia."
        )
    for summary in summaries:
        print(f"\n[{summary.name}] {summary.description}")
        print(
            f"- Període: {summary.start_date} — {summary.end_date}; "
            f"T actius: {summary.workers_t}"
        )
        print(
            f"- Cobertura: {summary.covered}/{summary.needs} "
            f"({summary.coverage_percent:.1f} %); "
            f"fase cobertura: {summary.coverage_phase_status}; "
            f"gap={_format_optional(summary.coverage_gap, '')}"
        )
        if summary.uncovered_need_ids:
            skill_text = ", ".join(
                f"{profile}={count}"
                for profile, count in summary.uncovered_by_skills
            )
            print(
                "- No cobertes: "
                f"sense candidat estàtic={summary.uncovered_without_candidate}; "
                f"amb candidats però incompatibles={summary.uncovered_with_candidates}; "
                f"habilitacions: {skill_text}"
            )
        print(
            "- Equitat: "
            f"hores={_format_optional(summary.annual_hours_range, ' h')}; "
            f"zona={_format_optional(summary.zone_rate_range_points, ' punts')}; "
            f"torn={_format_optional(summary.turn_rate_range_points, ' punts')}"
        )
        print(
            f"- Canvis: zona={_format_optional(summary.zone_changes)}; "
            f"torn={_format_optional(summary.turn_changes)}; "
            f"temps={summary.wall_time_seconds:.2f} s"
        )
        if summary.validation_errors:
            print(f"- Errors del validador: {summary.validation_errors}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                [asdict(summary) for summary in summaries],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nResum JSON: {args.json.resolve()}")

    return 0 if all(summary.validation_errors == 0 for summary in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
