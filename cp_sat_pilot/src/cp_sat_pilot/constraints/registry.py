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
    RuleSpec("rest_12h", "Solapaments i descans mínim de 12 h", "hard", "factibilitat"),
    RuleSpec("annual_hours", "Màxim anual d'hores", "hard", "factibilitat"),
)


SOFT_RULES = (
    RuleSpec("coverage", "Màxima cobertura", "objective", "cobertura"),
    RuleSpec("plan_stability", "Mínima alteració", "objective", "estabilitat_pla"),
    RuleSpec("consecutive_days", "Dies consecutius", "soft", "preferencies_operatives"),
    RuleSpec("friday", "Regla de divendres", "soft", "preferencies_operatives"),
    RuleSpec("preferred", "Assignació preferida", "soft", "preferencies_operatives"),
    RuleSpec("annual_equity", "Equitat anual d'hores", "soft", "equitat_oportunista"),
    RuleSpec("zone_equity", "Equitat de canvis de zona", "soft", "equitat_oportunista"),
    RuleSpec("turn_equity", "Equitat de canvis de torn", "soft", "equitat_oportunista"),
)


ALL_RULES = HARD_RULES + SOFT_RULES
