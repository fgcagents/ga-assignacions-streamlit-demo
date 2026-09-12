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
    RuleSpec(
        "friday_base_weekend",
        "Divendres tard abans de descans base de cap de setmana",
        "hard",
        "factibilitat",
    ),
)


SOFT_RULES = (
    RuleSpec("coverage", "Màxima cobertura", "objective", "cobertura"),
    RuleSpec(
        "covered_minutes",
        "Màxim de minuts coberts",
        "objective",
        "hores_cobertes",
    ),
    RuleSpec("plan_stability", "Mínima alteració", "objective", "estabilitat_pla"),
    RuleSpec("consecutive_days", "Dies consecutius", "diagnostic", "postanalisi"),
    RuleSpec("preferred", "Assignació preferida", "diagnostic", "postanalisi"),
    RuleSpec(
        "annual_equity",
        "Equitat sobre la referència contractual del 75%",
        "objective",
        "equitat_social",
    ),
    RuleSpec(
        "outside_preference",
        "Mínim de serveis fora de torn o zona",
        "objective",
        "equitat_social",
    ),
    RuleSpec(
        "night_equity",
        "Màxim nocturn acumulat mínim",
        "objective",
        "equitat_social",
    ),
    RuleSpec("zone_changes", "Canvis totals de zona", "diagnostic", "postanalisi"),
    RuleSpec("turn_changes", "Canvis totals de torn", "diagnostic", "postanalisi"),
)


ALL_RULES = HARD_RULES + SOFT_RULES
