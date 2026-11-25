"""
scheduling.py – Pure Python scheduling logic (no LLM).
Handles slot finding, focus block planning, health scoring, and conflict detection.
"""
from __future__ import annotations
from datetime import datetime, timedelta, date, time
from typing import Optional
import math

from models import Event, SlotOption, HealthReport

WORK_START = time(9, 0)
WORK_END = time(18, 0)
LUNCH_START = time(13, 0)
LUNCH_END = time(14, 0)
MIN_SLOT_GAP = 15   # minutes buffer between meetings


def _localize(dt: datetime) -> datetime:
    """Strip timezone for arithmetic when needed."""
    return dt.replace(tzinfo=None)


def _work_intervals(day: date) -> list[tuple[datetime, datetime]]:
    """Return working-hour intervals for a day, excluding lunch."""
    morning_start = datetime.combine(day, WORK_START)
    morning_end = datetime.combine(day, LUNCH_START)
    afternoon_start = datetime.combine(day, LUNCH_END)
    afternoon_end = datetime.combine(day, WORK_END)
    return [(morning_start, morning_end), (afternoon_start, afternoon_end)]


def _is_weekday(d: date) -> bool:
    return d.weekday() < 5


def _subtract_busy(free: list[tuple[datetime, datetime]],
                   busy_start: datetime, busy_end: datetime) -> list[tuple[datetime, datetime]]:
    """Remove a busy interval from a list of free intervals."""
    result = []
    for fs, fe in free:
        if busy_end <= fs or busy_start >= fe:
            result.append((fs, fe))
        elif busy_start <= fs and busy_end >= fe:
            pass  # fully consumed
        elif busy_start <= fs:
            result.append((busy_end, fe))
        elif busy_end >= fe:
            result.append((fs, busy_start))
        else:
            result.append((fs, busy_start))
            result.append((busy_end, fe))
    return result


def _events_on_day(events: list[Event], day: date) -> list[Event]:
    return [e for e in events if _localize(e.start).date() == day]


def find_slots(
    events: list[Event],
    attendee_busy: dict[str, list[tuple[datetime, datetime]]],
    duration_min: int,
    earliest: datetime,
    latest: datetime,
    prefer: Optional[str] = None,   # "morning" | "afternoon" | None
    count: int = 3,
) -> list[SlotOption]:
    """
    Find ranked slot options for a meeting.
    Respects working hours, skips lunch, avoids back-to-backs, avoids cutting focus blocks.
    """
    duration = timedelta(minutes=duration_min)
    buffer = timedelta(minutes=MIN_SLOT_GAP)
    candidates: list[SlotOption] = []

    # Build a sorted list of all existing meeting starts/ends for back-to-back detection
    all_busy: list[tuple[datetime, datetime]] = []
    for e in events:
        s = _localize(e.start)
        en = _localize(e.end)
        all_busy.append((s, en))
    # Add attendee busy times
    for periods in attendee_busy.values():
        for s, en in periods:
            all_busy.append((_localize(s), _localize(en)))

    current = _localize(earliest)
    end_limit = _localize(latest)

    while current < end_limit:
        day = current.date()
        if not _is_weekday(day):
            current = datetime.combine(day + timedelta(days=1), WORK_START)
            continue

        # Build free intervals for this day
        free_intervals = _work_intervals(day)
        for bs, be in all_busy:
            if bs.date() == day:
                free_intervals = _subtract_busy(free_intervals, bs - buffer, be + buffer)

        for fs, fe in sorted(free_intervals):
            slot_start = max(fs, current)
            slot_end = slot_start + duration

            if slot_end > fe:
                continue
            if slot_end > end_limit:
                continue

            # Score the slot
            score, reasons = _score_slot(slot_start, slot_end, events, all_busy, prefer)
            candidates.append(SlotOption(
                start=slot_start,
                end=slot_end,
                score=score,
                why="; ".join(reasons),
            ))

        # Advance to next day
        current = datetime.combine(day + timedelta(days=1), WORK_START)

    # De-duplicate and sort by score descending, then by time
    seen: set[datetime] = set()
    unique: list[SlotOption] = []
    for s in sorted(candidates, key=lambda x: (-x.score, x.start)):
        if s.start not in seen:
            seen.add(s.start)
            unique.append(s)

    return unique[:count]


def _score_slot(
    slot_start: datetime,
    slot_end: datetime,
    events: list[Event],
    all_busy: list[tuple[datetime, datetime]],
    prefer: Optional[str],
) -> tuple[float, list[str]]:
    score = 0.5
    reasons: list[str] = []

    hour = slot_start.hour

    # Preference bonus
    if prefer == "morning" and hour < 12:
        score += 0.2
        reasons.append("matches your morning preference")
    elif prefer == "afternoon" and hour >= 13:
        score += 0.2
        reasons.append("matches your afternoon preference")

    # Avoid late-day penalty
    if hour >= 17:
        score -= 0.15
        reasons.append("late in the day")
    elif hour < 10:
        score += 0.1
        reasons.append("early focused time")

    # Penalty for back-to-back
    buffer = timedelta(minutes=MIN_SLOT_GAP)
    for bs, be in all_busy:
        if abs((bs - slot_end).total_seconds()) < buffer.total_seconds():
            score -= 0.1
            reasons.append("adjacent to another event")
            break
        if abs((slot_start - be).total_seconds()) < buffer.total_seconds():
            score -= 0.1
            reasons.append("immediately after another event")
            break

    # Bonus for standalone (clear 30 min before and after)
    clear_window = timedelta(minutes=30)
    is_standalone = all(
        be + clear_window <= slot_start or bs >= slot_end + clear_window
        for bs, be in all_busy
    )
    if is_standalone:
        score += 0.15
        reasons.append("well-isolated slot")

    if not reasons:
        reasons.append("available slot within working hours")

    return min(max(score, 0.0), 1.0), reasons


def plan_focus_blocks(
    events: list[Event],
    hours_per_day: float,
    days: list[date],
    preferred_window: str = "morning",  # "morning" | "afternoon"
) -> list[Event]:
    """
    Fit focus blocks around existing meetings for each day.
    Respects working hours (09:00-18:00) and lunch (13:00-14:00).
    Morning window: 09:00 - 13:00 (4 hours).
    Afternoon window: 14:00 - 18:00 (4 hours).
    Adapts duration if a full block cannot fit continuously.
    """
    from models import Event as Ev
    proposed: list[Ev] = []

    # Strict window boundaries: do NOT bleed into afternoon if morning was requested!
    if preferred_window == "morning":
        search_windows = [(9, 13)]
    elif preferred_window == "afternoon":
        search_windows = [(14, 18)]
    else:
        search_windows = [(9, 13), (14, 18)]

    # Durations to attempt: requested duration first, then slightly shorter (down to 1h) if tight
    attempt_durations = [hours_per_day]
    if hours_per_day > 1.5:
        attempt_durations.append(1.5)
    if hours_per_day > 1.0:
        attempt_durations.append(1.0)

    for day in days:
        if not _is_weekday(day):
            continue

        busy_today = [(e.start.replace(tzinfo=None), e.end.replace(tzinfo=None))
                      for e in events if e.start.replace(tzinfo=None).date() == day]

        # Build free intervals for the day
        free_intervals = _work_intervals(day)
        buffer = timedelta(minutes=5)
        for bs, be in busy_today:
            free_intervals = _subtract_busy(free_intervals, bs - buffer, be + buffer)

        placed = False
        for dur_hours in attempt_durations:
            if placed:
                break
            duration = timedelta(hours=dur_hours)

            for s_hour, e_hour in search_windows:
                if placed:
                    break
                w_start = datetime.combine(day, time(s_hour, 0))
                w_end = datetime.combine(day, time(e_hour, 0))

                for fs, fe in sorted(free_intervals):
                    b_start = max(fs, w_start)
                    b_end = b_start + duration
                    if b_end <= fe and b_start >= w_start and b_end <= w_end:
                        h_label = f"{dur_hours:g}h"
                        proposed.append(Ev(
                            title=f"Focus Block ({h_label})",
                            start=b_start,
                            end=b_end,
                            type="focus",
                            description=f"Deep work block ({preferred_window or 'flexible'})",
                        ))
                        placed = True
                        break

    return proposed


def detect_conflicts(events: list[Event]) -> list[tuple[Event, Event]]:
    """Return pairs of overlapping events."""
    conflicts: list[tuple[Event, Event]] = []
    sorted_events = sorted(events, key=lambda e: e.start.replace(tzinfo=None))
    for i, e1 in enumerate(sorted_events):
        e1_end = e1.end.replace(tzinfo=None)
        for e2 in sorted_events[i + 1:]:
            e2_start = e2.start.replace(tzinfo=None)
            if e2_start >= e1_end:
                break
            # Both have same person / are calendar-owner events
            conflicts.append((e1, e2))
    return conflicts


def health_score(events: list[Event]) -> HealthReport:
    """
    Compute a 0-100 health score and flag issues.
    """
    issues: list[str] = []

    if not events:
        return HealthReport(
            score=100, meeting_hours=0, focus_hours=0,
            back_to_back_count=0, conflict_count=0,
            fragmentation_count=0, issues=["No events this week – great or calendar not loaded?"],
        )

    # Basic totals
    meeting_hours = sum(
        e.duration_minutes / 60 for e in events if e.type in ("meeting", "external")
    )
    focus_hours = sum(
        e.duration_minutes / 60 for e in events if e.type == "focus"
    )

    # Back-to-back detection (gap < 10 minutes)
    sorted_ev = sorted(events, key=lambda e: e.start.replace(tzinfo=None))
    btb = 0
    for i in range(len(sorted_ev) - 1):
        gap = (sorted_ev[i + 1].start.replace(tzinfo=None) - sorted_ev[i].end.replace(tzinfo=None)).total_seconds() / 60
        if 0 <= gap < 10:
            btb += 1

    # Conflicts
    conflicts = detect_conflicts(events)
    conflict_count = len(conflicts)

    # Fragmentation: gaps < 30 min between meetings on same day
    fragmentation = 0
    days: dict[date, list[Event]] = {}
    for e in events:
        d = e.start.replace(tzinfo=None).date()
        days.setdefault(d, []).append(e)
    for day_events in days.values():
        day_sorted = sorted(day_events, key=lambda e: e.start.replace(tzinfo=None))
        for i in range(len(day_sorted) - 1):
            gap = (day_sorted[i + 1].start.replace(tzinfo=None) - day_sorted[i].end.replace(tzinfo=None)).total_seconds() / 60
            if 0 < gap < 30:
                fragmentation += 1

    # Score penalties
    score = 100
    if meeting_hours > 20:
        score -= 20
        issues.append(f"Very high meeting load: {meeting_hours:.1f} hours this week")
    elif meeting_hours > 15:
        score -= 10
        issues.append(f"High meeting load: {meeting_hours:.1f} hours this week")

    if focus_hours < 2:
        score -= 15
        issues.append("Less than 2 hours of focus time scheduled")
    elif focus_hours < 4:
        score -= 5
        issues.append("Consider adding more focus blocks (currently < 4 h)")

    if btb > 3:
        score -= 10
        issues.append(f"{btb} back-to-back meetings – risk of fatigue")
    elif btb > 1:
        score -= 5
        issues.append(f"{btb} back-to-back meetings")

    if conflict_count > 0:
        score -= conflict_count * 8
        issues.append(f"{conflict_count} scheduling conflict(s) detected")

    if fragmentation > 4:
        score -= 8
        issues.append(f"{fragmentation} fragmented gaps (< 30 min) – hard to do real work")

    # Flag low-value events heuristically
    low_value = [e for e in events if _is_low_value(e)]
    if low_value:
        score -= min(len(low_value) * 3, 10)
        titles = ", ".join(e.title for e in low_value[:3])
        issues.append(f"Possibly unnecessary meetings: {titles}")

    score = max(0, min(100, score))
    return HealthReport(
        score=score,
        meeting_hours=round(meeting_hours, 1),
        focus_hours=round(focus_hours, 1),
        back_to_back_count=btb,
        conflict_count=conflict_count,
        fragmentation_count=fragmentation,
        issues=issues,
    )


def _is_low_value(event: Event) -> bool:
    """Heuristic: status syncs with no agenda / solo events."""
    low_value_keywords = ["status sync", "status update", "check-in", "standup"]
    if len(event.attendees) <= 1:
        return False
    title_lower = event.title.lower()
    return any(kw in title_lower for kw in low_value_keywords) and len(event.attendees) >= 4
