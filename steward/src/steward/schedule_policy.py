"""Schedule analysis and close-of-day diagnostics for resident declarations."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

from croniter import CroniterBadDateError, croniter

from steward.diagnostics import Diagnostic
from steward.manifest_models import (
    CLOSE_OF_DAY,
    MANY_FIRES_DISPLAY_THRESHOLD,
    ResidentManifest,
    Routine,
)


@cache
def _gregorian_cron_days() -> tuple[tuple[int, int, int], ...]:
    """Return every observable Gregorian ``(month, day, cron-weekday)`` tuple."""
    cycle_start = datetime(2000, 1, 1, tzinfo=UTC)
    representatives: set[tuple[int, int, int]] = set()
    for offset in range(146_097):  # exactly one 400-year Gregorian cycle
        day = cycle_start + timedelta(days=offset)
        representatives.add((day.month, day.day, (day.weekday() + 1) % 7))
    return tuple(sorted(representatives))


def _cron_values(field: Sequence[int | str], lowest: int, highest: int) -> set[int]:
    """Turn croniter's canonical field expansion into concrete matching values."""
    if "*" in field:
        return set(range(lowest, highest + 1))
    return {value for value in field if isinstance(value, int)}


def _daily_fire_range(routine: Routine) -> tuple[int, int]:
    return _daily_fire_range_for_schedule(routine.schedule)


@cache
def _daily_fire_range_for_schedule(schedule: str) -> tuple[int, int]:
    """Return the least and most fires over every distinct cron calendar day.

    A five-field cron date predicate can observe only month, day-of-month, and weekday.
    The Gregorian calendar repeats those alignments every 400 years (146,097 days), so
    one representative of each ``(month, day, weekday)`` tuple is exhaustive.  There are
    only 366 possible month/day pairs times seven weekdays: at most 2,562 probes rather
    than 146,097.  Fixed-offset datetimes are deliberate: cron names local wall-clock
    occurrences; DST resolution remains the scheduler's separate responsibility.
    """
    # croniter is the scheduler's semantic authority.  First let its iterator decide
    # whether the complete expression can ever fire.  This matters for expressions such
    # as February 31 with a weekday alternative: croniter considers the restricted,
    # impossible DOM unsatisfiable rather than applying the usual DOM/DOW union.
    try:
        croniter(schedule, datetime(2000, 1, 1, tzinfo=UTC)).get_next(datetime)
    except CroniterBadDateError:
        return 0, 0

    expanded, _ = croniter.expand(schedule)
    minute, hour, dom, month, dow = expanded
    fires_on_matching_day = len(_cron_values(minute, 0, 59)) * len(_cron_values(hour, 0, 23))
    months = _cron_values(month, 1, 12)
    month_days = _cron_values(dom, 1, 31)
    weekdays = _cron_values(dow, 0, 6)
    dom_wildcard = "*" in dom
    dow_wildcard = "*" in dow

    counts: list[int] = []
    for candidate_month, candidate_day, candidate_weekday in _gregorian_cron_days():
        date_matches = candidate_day in month_days
        weekday_matches = candidate_weekday in weekdays
        if dom_wildcard:
            calendar_matches = weekday_matches
        elif dow_wildcard:
            calendar_matches = date_matches
        else:
            calendar_matches = date_matches or weekday_matches
        counts.append(
            fires_on_matching_day if candidate_month in months and calendar_matches else 0
        )
    return min(counts), max(counts)


def _check_close_of_day(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    """One entry per day means one closing routine, firing once.

    Both halves are checked here rather than left to discover themselves at midnight: a
    second closer would write a second entry over the first, and a closer on an hourly
    schedule would rewrite the day twenty-four times and call the last one the day.
    """
    closers = [
        (index, routine)
        for index, routine in enumerate(manifest.routines)
        if routine.journal == CLOSE_OF_DAY
    ]
    diagnostics = [
        Diagnostic(
            file=source,
            field_path=f"routines[{index}].journal",
            problem=(
                f"routine {routine.id!r} also closes the day; "
                f"{', '.join(repr(r.id) for _, r in closers)} all claim it, and a day "
                f"that ends more than once is not a day"
            ),
            example=f"journal: {CLOSE_OF_DAY} on exactly one routine",
        )
        for index, routine in closers[1:]
    ]
    for index, routine in closers:
        if not routine.enabled:
            diagnostics.append(
                Diagnostic(
                    file=source,
                    field_path=f"routines[{index}].enabled",
                    problem=(
                        f"routine {routine.id!r} closes the day but is disabled and "
                        "therefore cannot close any day"
                    ),
                    example="enabled: true",
                )
            )
            continue
        least, most = _daily_fire_range(routine)
        if least == most == 1:
            continue
        if least == 0:
            cadence = "does not fire every day"
        elif least == most:
            many = (
                f"{MANY_FIRES_DISPLAY_THRESHOLD}+"
                if most > MANY_FIRES_DISPLAY_THRESHOLD
                else str(most)
            )
            cadence = f"fires {many} times a day"
        else:
            cadence = "fires more than once on some days"
        diagnostics.append(
            Diagnostic(
                file=source,
                field_path=f"routines[{index}].schedule",
                problem=(
                    f"routine {routine.id!r} closes the day but {cadence} in "
                    f"{routine.schedule_tz}; the journal is one entry per day, so the "
                    f"closing routine has to run exactly once every day"
                ),
                example="schedule: '30 22 * * *'  (once, late)",
            )
        )
    return diagnostics
