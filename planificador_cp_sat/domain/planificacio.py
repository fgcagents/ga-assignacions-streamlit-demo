"""Contracte únic d'entrada per a qualsevol execució de planificació."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Iterable


def _normalize_text_ids(
    values: Iterable[object],
    *,
    label: str,
) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{label} ha de ser una col·lecció d'identificadors")

    normalized: set[str] = set()
    for value in values:
        if value is None:
            raise ValueError(f"{label} no pot contenir identificadors nuls")
        identifier = str(value).strip()
        if not identifier:
            raise ValueError(f"{label} no pot contenir identificadors buits")
        normalized.add(identifier)
    return frozenset(normalized)


def _normalize_assignment_ids(values: Iterable[object]) -> frozenset[int]:
    if isinstance(values, (str, bytes)):
        raise TypeError(
            "assignment_ids ha de ser una col·lecció d'identificadors"
        )

    normalized: set[int] = set()
    for value in values:
        if isinstance(value, bool):
            raise ValueError(
                "assignment_ids només pot contenir enters positius"
            )
        try:
            identifier = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "assignment_ids només pot contenir enters positius"
            ) from error
        if identifier <= 0 or str(identifier) != str(value).strip():
            raise ValueError(
                "assignment_ids només pot contenir enters positius"
            )
        normalized.add(identifier)
    return frozenset(normalized)


def _validate_plain_date(value: object, *, label: str) -> None:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{label} ha de ser una data sense hora")


class PlanningTriggerKind(StrEnum):
    """Origen funcional que ha motivat una execució del planificador."""

    MANUAL = "manual"
    INCIDENT = "incidencia"
    DEMAND_CHANGE = "canvi_demanda"
    AVAILABILITY_CHANGE = "canvi_disponibilitat"


@dataclass(frozen=True, slots=True)
class PlanningScope:
    """Part del pla que es permet revisar en una execució.

    Una col·lecció buida significa que no s'aplica aquell filtre. Els filtres
    de treballadors i assignacions identifiquen els orígens que es poden
    revisar, no necessàriament tot el conjunt de possibles receptors.
    """

    start_date: date
    end_date: date
    worker_ids: frozenset[str] = field(default_factory=frozenset)
    service_ids: frozenset[str] = field(default_factory=frozenset)
    assignment_ids: frozenset[int] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        _validate_plain_date(self.start_date, label="start_date")
        _validate_plain_date(self.end_date, label="end_date")
        if self.start_date > self.end_date:
            raise ValueError("start_date no pot ser posterior a end_date")

        object.__setattr__(
            self,
            "worker_ids",
            _normalize_text_ids(self.worker_ids, label="worker_ids"),
        )
        object.__setattr__(
            self,
            "service_ids",
            _normalize_text_ids(self.service_ids, label="service_ids"),
        )
        object.__setattr__(
            self,
            "assignment_ids",
            _normalize_assignment_ids(self.assignment_ids),
        )


@dataclass(frozen=True, slots=True)
class ProtectionPolicy:
    """Proteccions addicionals aplicables al pla publicat.

    Els bloquejos explícits de la base de dades sempre són restriccions dures.
    ``freeze_until`` afegeix una finestra congelada amb límit inclusiu.
    """

    freeze_until: date | None = None
    protect_outside_scope: bool = True
    allow_unselected_workers_as_recipients: bool = False

    def __post_init__(self) -> None:
        if self.freeze_until is not None:
            _validate_plain_date(self.freeze_until, label="freeze_until")
        if not isinstance(self.protect_outside_scope, bool):
            raise TypeError("protect_outside_scope ha de ser booleà")
        if not self.protect_outside_scope:
            raise ValueError(
                "Les assignacions fora de l'abast sempre han de quedar "
                "protegides"
            )
        if not isinstance(
            self.allow_unselected_workers_as_recipients,
            bool,
        ):
            raise TypeError(
                "allow_unselected_workers_as_recipients ha de ser booleà"
            )

    def is_frozen(self, day: date) -> bool:
        """Indica si ``day`` cau dins de la finestra congelada inclusiva."""
        _validate_plain_date(day, label="day")
        return self.freeze_until is not None and day <= self.freeze_until


@dataclass(frozen=True, slots=True)
class PlanningTrigger:
    """Origen i afectació explícita que motiven una planificació."""

    kind: PlanningTriggerKind | str = PlanningTriggerKind.MANUAL
    source_id: str | int | None = None
    affected_need_ids: frozenset[str] = field(default_factory=frozenset)
    reason: str | None = None

    def __post_init__(self) -> None:
        try:
            kind = PlanningTriggerKind(self.kind)
        except ValueError as error:
            allowed = ", ".join(item.value for item in PlanningTriggerKind)
            raise ValueError(
                f"Origen de planificació desconegut; valors admesos: {allowed}"
            ) from error
        object.__setattr__(self, "kind", kind)

        source_id = None
        if self.source_id is not None:
            source_id = str(self.source_id).strip()
            if not source_id:
                raise ValueError("source_id no pot ser buit")
        if kind is PlanningTriggerKind.INCIDENT and source_id is None:
            raise ValueError("Una incidència ha d'indicar source_id")
        object.__setattr__(self, "source_id", source_id)

        object.__setattr__(
            self,
            "affected_need_ids",
            _normalize_text_ids(
                self.affected_need_ids,
                label="affected_need_ids",
            ),
        )

        reason = None
        if self.reason is not None:
            if not isinstance(self.reason, str):
                raise TypeError("reason ha de ser text")
            reason = self.reason.strip()
            if not reason:
                raise ValueError("reason no pot ser buit")
        object.__setattr__(self, "reason", reason)


def _normalize_worker_dates(
    values: Iterable[tuple[object, object]],
    *,
    label: str,
) -> frozenset[tuple[str, date]]:
    normalized: set[tuple[str, date]] = set()
    for value in values:
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise ValueError(f"{label} ha de contenir parelles treballador-data")
        worker_id = str(value[0]).strip()
        if not worker_id:
            raise ValueError(f"{label} no pot contenir treballadors buits")
        day = value[1]
        _validate_plain_date(day, label=f"{label}.data")
        normalized.add((worker_id, day))
    return frozenset(normalized)


def _normalize_preferences(
    values: Iterable[tuple[object, object]],
) -> tuple[tuple[str, str], ...]:
    normalized: dict[str, str] = {}
    for value in values:
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise ValueError(
                "preferred_assignments ha de contenir parelles necessitat-treballador"
            )
        need_id = str(value[0]).strip()
        worker_id = str(value[1]).strip()
        if not need_id or not worker_id:
            raise ValueError(
                "preferred_assignments no pot contenir identificadors buits"
            )
        previous = normalized.setdefault(need_id, worker_id)
        if previous != worker_id:
            raise ValueError(
                f"La necessitat {need_id} té més d'un treballador preferit"
            )
    return tuple(sorted(normalized.items()))


@dataclass(frozen=True, slots=True)
class PlanningInputAdjustments:
    """Transformacions reproduïbles de disponibilitat i preferència."""

    unavailable_worker_dates: frozenset[tuple[str, date]] = field(
        default_factory=frozenset
    )
    released_worker_dates: frozenset[tuple[str, date]] = field(
        default_factory=frozenset
    )
    preferred_assignments: tuple[tuple[str, str], ...] = ()
    allow_active_assignments_without_coverage: bool = False

    def __post_init__(self) -> None:
        unavailable = _normalize_worker_dates(
            self.unavailable_worker_dates,
            label="unavailable_worker_dates",
        )
        released = _normalize_worker_dates(
            self.released_worker_dates,
            label="released_worker_dates",
        )
        overlap = unavailable & released
        if overlap:
            worker_id, day = sorted(overlap)[0]
            raise ValueError(
                f"La disponibilitat de {worker_id} el {day.isoformat()} és contradictòria"
            )
        object.__setattr__(self, "unavailable_worker_dates", unavailable)
        object.__setattr__(self, "released_worker_dates", released)
        object.__setattr__(
            self,
            "preferred_assignments",
            _normalize_preferences(self.preferred_assignments),
        )
        if not isinstance(
            self.allow_active_assignments_without_coverage,
            bool,
        ):
            raise TypeError(
                "allow_active_assignments_without_coverage ha de ser booleà"
            )


@dataclass(frozen=True, slots=True)
class PlanningExecutionRequest:
    """Petició única consumida pel futur servei de planificació."""

    scope: PlanningScope
    protection: ProtectionPolicy = field(default_factory=ProtectionPolicy)
    trigger: PlanningTrigger = field(default_factory=PlanningTrigger)
    adjustments: PlanningInputAdjustments = field(
        default_factory=PlanningInputAdjustments
    )

    def __post_init__(self) -> None:
        if not isinstance(self.scope, PlanningScope):
            raise TypeError("scope ha de ser PlanningScope")
        if not isinstance(self.protection, ProtectionPolicy):
            raise TypeError("protection ha de ser ProtectionPolicy")
        if not isinstance(self.trigger, PlanningTrigger):
            raise TypeError("trigger ha de ser PlanningTrigger")
        if not isinstance(self.adjustments, PlanningInputAdjustments):
            raise TypeError("adjustments ha de ser PlanningInputAdjustments")
