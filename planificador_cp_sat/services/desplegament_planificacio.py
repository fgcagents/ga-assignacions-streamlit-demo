"""Configuració i telemetria del desplegament incremental."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from planificador_cp_sat.services.persistencia_planificacio import (
    StoredPlanningExecution,
)


class PlanningRolloutMode(StrEnum):
    SHADOW = "shadow"
    PILOT = "pilot"
    ACTIVE = "active"


@dataclass(frozen=True, slots=True)
class PlanningRolloutConfig:
    mode: PlanningRolloutMode
    publication_enabled: bool

    @property
    def is_shadow(self) -> bool:
        return self.mode is PlanningRolloutMode.SHADOW


def _boolean(value: str, *, variable: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "si", "sí"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{variable} ha de ser un valor booleà")


def load_planning_rollout_config(
    environment: Mapping[str, str] | None = None,
) -> PlanningRolloutConfig:
    """Carrega el mode; per defecte, la interfície opera en ombra."""
    values = os.environ if environment is None else environment
    raw_mode = values.get("PLANIFICACIO_INCREMENTAL_MODE", "shadow")
    try:
        mode = PlanningRolloutMode(raw_mode.strip().lower())
    except ValueError as error:
        raise ValueError(
            "PLANIFICACIO_INCREMENTAL_MODE ha de ser shadow, pilot o active"
        ) from error
    default_publication = mode is not PlanningRolloutMode.SHADOW
    raw_publication = values.get("PLANIFICACIO_INCREMENTAL_PUBLICATION_ENABLED")
    publication_enabled = (
        default_publication
        if raw_publication is None
        else _boolean(
            raw_publication,
            variable="PLANIFICACIO_INCREMENTAL_PUBLICATION_ENABLED",
        )
    )
    if mode is PlanningRolloutMode.SHADOW and publication_enabled:
        raise ValueError(
            "El mode shadow no pot habilitar la publicació incremental"
        )
    return PlanningRolloutConfig(
        mode=mode,
        publication_enabled=publication_enabled,
    )


def planning_shadow_report(execution: StoredPlanningExecution) -> dict:
    """Resumeix cobertura, estabilitat i temps d'una proposta desada."""
    wall_time = execution.metrics.get("wall_time_seconds")
    return {
        "execution_id": execution.id,
        "state": execution.state,
        "start_date": execution.request.scope.start_date.isoformat(),
        "end_date": execution.request.scope.end_date.isoformat(),
        "covered_needs": execution.covered_needs,
        "total_needs": execution.total_needs,
        "coverage_percent": (
            100.0
            if not execution.total_needs
            else round(
                100 * execution.covered_needs / execution.total_needs,
                2,
            )
        ),
        "unchanged_assignments": execution.unchanged_assignments,
        "persistent_changes": execution.persistent_changes,
        "uncovered_needs": execution.uncovered_needs,
        "wall_time_seconds": wall_time,
        "snapshot_hash": execution.snapshot_hash,
        "result_hash": execution.result_hash,
    }
