"""Paràmetres editables de preferències; no relaxen restriccions dures."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PriorityPolicy:
    zone_streak_enabled: bool = True
    zone_streak_threshold: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.zone_streak_enabled, bool):
            raise ValueError("L'activació de ratxes ha de ser un booleà")
        if (
            isinstance(self.zone_streak_threshold, bool)
            or not isinstance(self.zone_streak_threshold, int)
            or self.zone_streak_threshold < 0
        ):
            raise ValueError("El llindar de ratxes ha de ser un enter no negatiu")
