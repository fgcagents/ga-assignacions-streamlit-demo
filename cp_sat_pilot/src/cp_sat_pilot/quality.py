from __future__ import annotations

from dataclasses import dataclass

from .domain import SolveResult


REQUIRED_PHASE_STATUS = "OPTIMAL"
MAX_INFORMATIONAL_ALERT_SHARE_PERCENT = 25


@dataclass(frozen=True, slots=True)
class EquityExecutionAssessment:
    """Resum de factibilitat i del grau d'optimalitat assolit."""

    status: str
    publishable: bool
    operational_phase_status: str
    equity_phase_status: str
    reasons: tuple[str, ...]
    principle: str = "referencia_contractual_75_informativa"
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
    """Permet publicar solucions factibles i informa de l'optimalitat."""

    coverage_status = _phase_status(result, "cobertura")
    hours_status = _phase_status(result, "hores_cobertes")
    equity_status = _phase_status(result, "equitat_social")
    reasons: list[str] = []
    if coverage_status != REQUIRED_PHASE_STATUS:
        reasons.append("cobertura_no_resolta")
    if hours_status != REQUIRED_PHASE_STATUS:
        reasons.append("hores_cobertes_no_optimitzades")
    if equity_status != REQUIRED_PHASE_STATUS:
        reasons.append("equitat_social_no_optimitzada")
    if result.soft_metrics is None:
        reasons.append("metriques_equitat_no_disponibles")
    if result.validation_errors:
        reasons.append("errors_restriccions_dures")
    optimality_certified = (
        result.feasible
        and not result.validation_errors
        and coverage_status == REQUIRED_PHASE_STATUS
        and hours_status == REQUIRED_PHASE_STATUS
        and equity_status == REQUIRED_PHASE_STATUS
        and result.soft_metrics is not None
    )
    publishable = result.feasible and not result.validation_errors

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
            if not result.feasible or result.validation_errors
            else "factible_no_optima"
            if not optimality_certified
            else "avaluada_amb_alertes"
            if alert_worker_ids
            else "avaluada"
        ),
        publishable=publishable,
        operational_phase_status=hours_status,
        equity_phase_status=equity_status,
        reasons=tuple(reasons),
        technical_ready=optimality_certified,
        requires_manual_review=False,
        review_worker_ids=alert_worker_ids,
        review_reasons=(),
        comparable_workers=comparable_workers,
        max_manual_review_workers=0,
        manual_override_allowed=False,
        broad_review_warning=broad_warning,
        review_share_percent=alert_share_percent,
    )
