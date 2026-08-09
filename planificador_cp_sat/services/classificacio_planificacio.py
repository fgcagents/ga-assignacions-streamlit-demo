"""Classificació única de les assignacions del pla de referència."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from planificador_cp_sat.domain import PlanningExecutionRequest
from planificador_cp_sat.services.fotografia_planificacio import (
    ActiveAssignmentSnapshot,
    PlanningSnapshot,
)


class AssignmentClassificationError(ValueError):
    """Indica que la política produeix una classificació contradictòria."""


class AssignmentCategory(StrEnum):
    """Categoria operativa assignada a cada cobertura publicada."""

    EXPLICIT_LOCK = "bloquejada_explicitament"
    FROZEN = "congelada"
    OUTSIDE_SCOPE = "fora_abast"
    AFFECTED = "afectada"
    MODIFIABLE = "modificable"


HARD_LOCKED_CATEGORIES = frozenset(
    {
        AssignmentCategory.EXPLICIT_LOCK,
        AssignmentCategory.FROZEN,
        AssignmentCategory.OUTSIDE_SCOPE,
    }
)


@dataclass(frozen=True, slots=True)
class ClassifiedAssignment:
    """Assignació, categoria i explicació presentable a l'usuari."""

    assignment: ActiveAssignmentSnapshot
    category: AssignmentCategory
    reason_code: str
    reason: str

    @property
    def is_hard_locked(self) -> bool:
        return self.category in HARD_LOCKED_CATEGORIES

    @property
    def can_change(self) -> bool:
        return self.category in {
            AssignmentCategory.AFFECTED,
            AssignmentCategory.MODIFIABLE,
        }

    @property
    def has_stability_penalty(self) -> bool:
        return self.category is AssignmentCategory.MODIFIABLE


@dataclass(frozen=True, slots=True)
class AssignmentClassification:
    """Classificació completa vinculada a una fotografia immutable."""

    snapshot_fingerprint: str
    assignments: tuple[ClassifiedAssignment, ...]
    affected_need_ids: frozenset[str]

    @property
    def hard_locked_need_ids(self) -> frozenset[str]:
        return frozenset(
            item.assignment.need_id
            for item in self.assignments
            if item.is_hard_locked and not item.assignment.is_boundary
        )

    @property
    def penalized_need_ids(self) -> frozenset[str]:
        return frozenset(
            item.assignment.need_id
            for item in self.assignments
            if item.has_stability_penalty
        )

    @property
    def modifiable_need_ids(self) -> frozenset[str]:
        return frozenset(
            item.assignment.need_id
            for item in self.assignments
            if item.can_change
        )

    def by_assignment_id(self) -> dict[int, ClassifiedAssignment]:
        return {
            item.assignment.assignment_id: item
            for item in self.assignments
        }


def _protected_classification(
    assignment: ActiveAssignmentSnapshot,
    request: PlanningExecutionRequest,
) -> tuple[AssignmentCategory, str, str] | None:
    if assignment.state == "bloquejada":
        return (
            AssignmentCategory.EXPLICIT_LOCK,
            "bloqueig_explicit",
            "Assignació bloquejada explícitament al pla operatiu.",
        )
    if assignment.state != "publicada":
        raise AssignmentClassificationError(
            f"L'assignació #{assignment.assignment_id} té l'estat actiu "
            f"desconegut {assignment.state}"
        )
    if request.protection.is_frozen(assignment.date):
        limit = request.protection.freeze_until
        return (
            AssignmentCategory.FROZEN,
            "finestra_congelada",
            "Assignació dins de la finestra congelada fins al "
            f"{limit.strftime('%d/%m/%Y')} inclòs.",
        )
    if not assignment.in_scope:
        if assignment.is_boundary:
            reason = (
                "Assignació de frontera protegida per validar descansos i "
                "incompatibilitats."
            )
            reason_code = "frontera_temporal"
        else:
            reason = (
                "Assignació protegida perquè queda fora dels filtres de "
                "l'execució."
            )
            reason_code = "fora_abast"
        return AssignmentCategory.OUTSIDE_SCOPE, reason_code, reason
    return None


def _validate_affected_needs(
    snapshot: PlanningSnapshot,
    request: PlanningExecutionRequest,
) -> None:
    needs_by_id = {item.need.id: item for item in snapshot.needs}
    unknown = request.trigger.affected_need_ids - set(needs_by_id)
    if unknown:
        raise AssignmentClassificationError(
            "Necessitats afectades desconegudes a la fotografia: "
            + ", ".join(sorted(unknown))
        )
    outside_scope = {
        need_id
        for need_id in request.trigger.affected_need_ids
        if not needs_by_id[need_id].in_scope
    }
    if outside_scope:
        raise AssignmentClassificationError(
            "Les necessitats afectades han de quedar dins de l'abast: "
            + ", ".join(sorted(outside_scope))
        )


def classify_snapshot_assignments(
    snapshot: PlanningSnapshot,
    request: PlanningExecutionRequest,
) -> AssignmentClassification:
    """Classifica cada assignació exactament una vegada i conserva el motiu."""
    if not isinstance(snapshot, PlanningSnapshot):
        raise TypeError("snapshot ha de ser PlanningSnapshot")
    if not isinstance(request, PlanningExecutionRequest):
        raise TypeError("request ha de ser PlanningExecutionRequest")
    if snapshot.scope != request.scope:
        raise AssignmentClassificationError(
            "L'abast de la petició no coincideix amb el de la fotografia"
        )

    _validate_affected_needs(snapshot, request)
    classified: list[ClassifiedAssignment] = []
    for assignment in snapshot.all_assignments:
        protected = _protected_classification(assignment, request)
        is_affected = assignment.need_id in request.trigger.affected_need_ids
        if protected is not None:
            category, reason_code, reason = protected
            if is_affected:
                raise AssignmentClassificationError(
                    f"La necessitat {assignment.need_id} està afectada i "
                    f"també protegida per {reason_code}"
                )
        elif is_affected:
            category = AssignmentCategory.AFFECTED
            reason_code = "afectada_explicitament"
            reason = (
                "Necessitat afectada explícitament per "
                f"{request.trigger.kind.value}; es pot modificar sense "
                "penalització d'estabilitat."
            )
        else:
            category = AssignmentCategory.MODIFIABLE
            reason_code = "publicada_modificable"
            reason = (
                "Assignació publicada modificable amb penalització per "
                "qualsevol canvi."
            )
        classified.append(
            ClassifiedAssignment(
                assignment=assignment,
                category=category,
                reason_code=reason_code,
                reason=reason,
            )
        )

    return AssignmentClassification(
        snapshot_fingerprint=snapshot.fingerprint,
        assignments=tuple(classified),
        affected_need_ids=request.trigger.affected_need_ids,
    )
