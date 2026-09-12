"""Perfils visuals sobre els pesos existents del motor prioritari."""

from dataclasses import replace

from cp_sat_pilot import SolverConfig


PREFERENCE_PROFILES = {
    "torn": ("Prioritzar torn", 3, 1),
    "zona": ("Prioritzar zona", 1, 3),
    "igual": ("Mateix pes", 1, 1),
}


def preference_weights(profile):
    if profile not in PREFERENCE_PROFILES:
        raise ValueError("Prioritat de preferències desconeguda")
    _, turn, zone = PREFERENCE_PROFILES[profile]
    return replace(SolverConfig().soft_weights,
                   accumulated_turn_equity=turn, accumulated_zone_equity=zone)


def preference_profile(weights):
    pair = (weights.accumulated_turn_equity, weights.accumulated_zone_equity)
    return next((key for key, (_, turn, zone) in PREFERENCE_PROFILES.items()
                 if pair == (turn, zone)), "personalitzada")
