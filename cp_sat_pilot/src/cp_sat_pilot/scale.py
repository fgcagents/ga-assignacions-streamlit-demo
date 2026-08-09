from __future__ import annotations

import ctypes
import sys
from collections import Counter
from dataclasses import replace
from datetime import timedelta

from .domain import Need, PlanningProblem


def build_scaled_problem(
    problem: PlanningProblem,
    *,
    copies: int = 3,
) -> PlanningProblem:
    """
    Replica consecutivament demanda, descansos i exclusions.

    Les assignacions històriques que coincideixen amb el nou horitzó es
    retiren en memòria i els seus comptadors es descompten, igual que fa
    `replace_all` per al període original.
    """
    if copies <= 0:
        raise ValueError("El nombre de còpies ha de ser positiu")
    if not problem.needs:
        raise ValueError("No es pot escalar un problema sense necessitats")

    first_date = min(need.date for need in problem.needs)
    last_date = max(need.date for need in problem.needs)
    block_days = (last_date - first_date).days + 1
    offsets = tuple(timedelta(days=index * block_days) for index in range(copies))

    scaled_needs = tuple(
        Need(
            id=f"bloc_{block_index + 1}__{need.id}",
            service_id=need.service_id,
            date=need.date + offset,
            start=need.start + offset,
            end=need.end + offset,
            required_skills=need.required_skills,
            zone=need.zone,
            turn_options=need.turn_options,
        )
        for block_index, offset in enumerate(offsets)
        for need in problem.needs
    )
    scaled_dates = frozenset(need.date for need in scaled_needs)
    base_dates = frozenset(need.date for need in problem.needs)

    removed_history = tuple(
        assignment
        for assignment in problem.history
        if assignment.start.date() in scaled_dates
    )
    removed_minutes = Counter()
    removed_assignments = Counter()
    removed_zone_changes = Counter()
    removed_turn_changes = Counter()
    for assignment in removed_history:
        removed_minutes[assignment.worker_id] += assignment.duration_minutes
        removed_assignments[assignment.worker_id] += 1
        removed_zone_changes[assignment.worker_id] += int(assignment.zone_change)
        removed_turn_changes[assignment.worker_id] += int(assignment.turn_change)

    workers = tuple(
        replace(
            worker,
            rest_dates=frozenset(
                {
                    rest_date
                    for rest_date in worker.rest_dates
                    if rest_date not in scaled_dates
                }
            ).union(
                {
                    rest_date + offset
                    for rest_date in worker.rest_dates
                    if rest_date in base_dates
                    for offset in offsets
                }
            ),
            annual_minutes=max(
                0,
                worker.annual_minutes - removed_minutes[worker.id],
            ),
            historical_assignments=max(
                0,
                worker.historical_assignments
                - removed_assignments[worker.id],
            ),
            historical_zone_changes=max(
                0,
                worker.historical_zone_changes
                - removed_zone_changes[worker.id],
            ),
            historical_turn_changes=max(
                0,
                worker.historical_turn_changes
                - removed_turn_changes[worker.id],
            ),
        )
        for worker in problem.workers
    )

    base_exclusions = tuple(
        exclusion
        for exclusion in problem.exclusions
        if exclusion[1] in base_dates
    )
    exclusions_outside_horizon = {
        exclusion
        for exclusion in problem.exclusions
        if exclusion[1] not in scaled_dates
    }
    scaled_exclusions = frozenset(exclusions_outside_horizon).union(
        {
            (worker_id, exclusion_date + offset)
            for worker_id, exclusion_date in base_exclusions
            for offset in offsets
        }
    )

    return PlanningProblem(
        workers=workers,
        needs=scaled_needs,
        history=tuple(
            assignment
            for assignment in problem.history
            if assignment not in removed_history
        ),
        exclusions=scaled_exclusions,
    )


def peak_working_set_bytes() -> int | None:
    """Retorna el pic de memòria del procés a Windows, si està disponible."""
    if sys.platform != "win32":
        return None

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCounters),
        ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    handle = kernel32.GetCurrentProcess()
    success = psapi.GetProcessMemoryInfo(
        handle,
        ctypes.byref(counters),
        counters.cb,
    )
    return int(counters.PeakWorkingSetSize) if success else None
