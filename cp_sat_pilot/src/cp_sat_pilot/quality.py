from __future__ import annotations

from dataclasses import dataclass

from .domain import SolveResult


SOLVED_PHASE_STATUSES = frozenset({"FEASIBLE", "OPTIMAL"})
MAX_INFORMATIONAL_ALERT_SHARE_PERCENT = 25


@dataclass(frozen=True, slots=True)
class EquityExecutionAssessment:
    """Resum informatiu de l'equitat calculada.

    Aquesta avaluació no participa en la selecció de solucions, no provoca
    reintents i no bloqueja la validació ni la publicació.
    """

    status: str
    publishable: bool
    operational_phase_status: str
    equity_phase_status: str
    reasons: tuple[str, ...]
    principle: str = "referencia_contractual_75_diagnostic_no_bloquejant"
    technical_ready: bool = False
    requires_manual_review: bool = False
    review_worker_ids: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()
    comparable_workers: int = 0
    max_manual_review_workers: int = 0
    manual_override_allowed: bool = False
    broad_review_warning: bool = False
    review_share_percent: float = 0.0


def _phase_status(result: SolveResult, name: str) -> str:
    return next(
        (
            phase.status
            for phase in result.optimization_phases
            if phase.name == name
        ),
        "NO_EXECUTADA",
    )


def assess_equity_execution(result: SolveResult) -> EquityExecutionAssessment:
    """Descriu el resultat sense convertir les alertes en una porta."""

    coverage_status = _phase_status(result, "cobertura")
    equity_status = _phase_status(result, "equitat_hores_contractual")
    changes_status = _phase_status(result, "desempat_canvis")
    publishable = result.feasible and not result.validation_errors
    technical_ready = publishable
    reasons: list[str] = []
    if coverage_status not in SOLVED_PHASE_STATUSES:
        reasons.append("cobertura_no_resolta")
    if equity_status not in SOLVED_PHASE_STATUSES:
        reasons.append("equitat_contractual_no_optimitzada")
    if changes_status not in SOLVED_PHASE_STATUSES:
        reasons.append("desempat_canvis_no_optimitzat")
    if result.soft_metrics is None:
        reasons.append("metriques_equitat_no_disponibles")
    if result.validation_errors:
        reasons.append("errors_restriccions_dures")

    alert_worker_ids = tuple(
        sorted(
            item.worker_id
            for item in result.equity_diagnostics
            if item.review_status == "alerta_informativa"
        )
    )
    comparable_workers = sum(
        item.comparable for item in result.equity_diagnostics
    )
    alert_share_percent = (
        round(100 * len(alert_worker_ids) / comparable_workers, 1)
        if comparable_workers
        else 0.0
    )
    broad_warning = bool(
        comparable_workers
        and alert_share_percent > MAX_INFORMATIONAL_ALERT_SHARE_PERCENT
    )
    if alert_worker_ids:
        reasons.append("desviacions_informatives")
    if broad_warning:
        reasons.append("desviacions_informatives_abast_ampli")

    return EquityExecutionAssessment(
        status=(
            "no_factible"
            if not publishable
            else "avaluada_amb_alertes"
            if alert_worker_ids or equity_status not in SOLVED_PHASE_STATUSES
            else "avaluada"
        ),
        publishable=publishable,
        operational_phase_status=changes_status,
        equity_phase_status=equity_status,
        reasons=tuple(reasons),
        technical_ready=technical_ready,
        requires_manual_review=False,
        review_worker_ids=alert_worker_ids,
        review_reasons=(),
        comparable_workers=comparable_workers,
        max_manual_review_workers=0,
        manual_override_allowed=False,
        broad_review_warning=broad_warning,
        review_share_percent=alert_share_percent,
    )
