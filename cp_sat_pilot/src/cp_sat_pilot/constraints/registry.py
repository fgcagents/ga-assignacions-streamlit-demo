from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuleSpec:
    code: str
    name: str
    kind: str
    phase: str


HARD_RULES = (
    RuleSpec("candidate", "Candidat compatible", "hard", "factibilitat"),
    RuleSpec("need_once", "Una persona per necessitat", "hard", "factibilitat"),
    RuleSpec("locked", "Assignacions bloquejades", "hard", "factibilitat"),
    RuleSpec("person_day", "Una assignació per persona i dia", "hard", "factibilitat"),
    RuleSpec(
        "max_consecutive_days",
        "Màxim 11 dies consecutius",
        "hard",
        "factibilitat",
    ),
    RuleSpec("rest_12h", "Solapaments i descans mínim de 12 h", "hard", "factibilitat"),
    RuleSpec("annual_hours", "Màxim anual d'hores", "hard", "factibilitat"),
)


SOFT_RULES = (
    RuleSpec("coverage", "Màxima cobertura", "objective", "cobertura"),
    RuleSpec("plan_stability", "Mínima alteració", "objective", "estabilitat_pla"),
    RuleSpec("consecutive_days", "Dies consecutius", "diagnostic", "postanalisi"),
    RuleSpec("friday", "Regla de divendres", "diagnostic", "postanalisi"),
    RuleSpec("preferred", "Assignació preferida", "diagnostic", "postanalisi"),
    RuleSpec(
        "annual_equity",
        "Equitat sobre la referència contractual del 75%",
        "objective",
        "equitat_hores_contractual",
    ),
    RuleSpec("zone_changes", "Canvis totals de zona", "tiebreak", "desempat_canvis"),
    RuleSpec("turn_changes", "Canvis totals de torn", "tiebreak", "desempat_canvis"),
)


ALL_RULES = HARD_RULES + SOFT_RULES
