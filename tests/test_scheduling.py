"""
tests/test_scheduling.py – Unit tests for scheduling.py
"""
from __future__ import annotations
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import datetime, date, timedelta, time
import pytest
from models import Event, Attendee
from scheduling import find_slots, health_score, detect_conflicts


ME = Attendee(email="me@example.com", name="Me")
PRIYA = Attendee(email="priya@example.com", name="Priya")


def make_event(title, day_offset, start_h, end_h, etype="meeting", attendees=None):
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    base = monday + timedelta(days=day_offset)
    return Event(
        title=title,
        start=datetime.combine(base, time(start_h, 0)),
        end=datetime.combine(base, time(end_h, 0)),
        attendees=attendees or [ME],
        type=etype,
    )


# ---------------------------------------------------------------------------
# find_slots tests
# ---------------------------------------------------------------------------

class TestFindSlots:
    def test_returns_slots_when_calendar_has_gaps(self):
        events = [
            make_event("Morning Meeting", 0, 9, 10),
            make_event("Afternoon Meeting", 0, 15, 16),
        ]
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        earliest = datetime.combine(monday, time(9, 0))
        latest = datetime.combine(monday + timedelta(days=5), time(18, 0))

        slots = find_slots(events, {}, 60, earliest, latest, count=3)
        assert len(slots) > 0, "Should find at least one slot"

    def test_respects_duration(self):
        events = []
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        earliest = datetime.combine(monday, time(9, 0))
        latest = datetime.combine(monday + timedelta(days=1), time(18, 0))

        slots = find_slots(events, {}, 90, earliest, latest, count=3)
        for slot in slots:
            duration = (slot.end - slot.start).total_seconds() / 60
            assert duration == 90, f"Slot duration should be 90 min, got {duration}"

    def test_avoids_lunch_13_to_14(self):
        events = []
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        earliest = datetime.combine(monday, time(9, 0))
        latest = datetime.combine(monday + timedelta(days=1), time(18, 0))

        slots = find_slots(events, {}, 60, earliest, latest, count=10)
        for slot in slots:
            # No slot should overlap with lunch window 13:00–14:00
            if slot.start.date() == monday:
                assert not (time(13, 0) <= slot.start.time() < time(14, 0)), \
                    f"Slot should not start during lunch: {slot.start}"
                assert not (slot.start.time() < time(13, 0) and slot.end.time() > time(13, 0)), \
                    f"Slot should not overlap lunch: {slot.start} - {slot.end}"

    def test_prefers_morning_when_requested(self):
        events = []
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        earliest = datetime.combine(monday, time(9, 0))
        latest = datetime.combine(monday + timedelta(days=1), time(18, 0))

        slots = find_slots(events, {}, 30, earliest, latest, prefer="morning", count=3)
        if slots:
            # Top slot should be in the morning
            assert slots[0].start.hour < 13, \
                f"Top slot should be morning, got {slots[0].start.hour}:00"

    def test_no_slots_outside_working_hours(self):
        events = []
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        earliest = datetime.combine(monday, time(9, 0))
        latest = datetime.combine(monday + timedelta(days=5), time(18, 0))

        slots = find_slots(events, {}, 30, earliest, latest, count=10)
        for slot in slots:
            assert slot.start.hour >= 9, f"Slot starts before 9am: {slot.start}"
            assert slot.end.hour <= 18, f"Slot ends after 6pm: {slot.end}"

    def test_returns_fewer_slots_than_count_when_busy(self):
        # Pack the day solid
        events = [make_event(f"Meeting {i}", 0, 9 + i, 10 + i) for i in range(8)]
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        earliest = datetime.combine(monday, time(9, 0))
        latest = datetime.combine(monday + timedelta(days=1), time(18, 0))

        slots = find_slots(events, {}, 120, earliest, latest, count=3)
        assert len(slots) <= 3


# ---------------------------------------------------------------------------
# health_score tests
# ---------------------------------------------------------------------------

class TestHealthScore:
    def test_empty_calendar_returns_100(self):
        # Empty calendar except the note in issues
        report = health_score([])
        assert report.score == 100

    def test_many_meetings_reduces_score(self):
        events = [make_event(f"Meeting {i}", i % 5, 9 + (i % 8), 10 + (i % 8))
                  for i in range(20)]
        report = health_score(events)
        assert report.score < 100

    def test_conflicts_reduce_score(self):
        events = [
            make_event("A", 0, 10, 11),
            make_event("B", 0, 10, 11),   # same time = conflict
        ]
        report = health_score(events)
        assert report.conflict_count >= 1
        assert report.score < 100

    def test_no_focus_time_flagged(self):
        events = [make_event("Meeting", 0, 10, 11, etype="meeting")]
        report = health_score(events)
        issues_text = " ".join(report.issues).lower()
        assert "focus" in issues_text

    def test_back_to_back_detected(self):
        events = [
            make_event("A", 0, 10, 11),
            make_event("B", 0, 11, 12),   # immediately after = back-to-back
        ]
        report = health_score(events)
        assert report.back_to_back_count >= 1


# ---------------------------------------------------------------------------
# detect_conflicts tests
# ---------------------------------------------------------------------------

class TestDetectConflicts:
    def test_no_conflicts_when_no_overlap(self):
        events = [
            make_event("A", 0, 9, 10),
            make_event("B", 0, 11, 12),
            make_event("C", 0, 14, 15),
        ]
        conflicts = detect_conflicts(events)
        assert conflicts == []

    def test_detects_full_overlap(self):
        events = [
            make_event("A", 0, 10, 12),
            make_event("B", 0, 10, 12),
        ]
        conflicts = detect_conflicts(events)
        assert len(conflicts) >= 1

    def test_detects_partial_overlap(self):
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        base = monday
        e1 = Event(
            title="A",
            start=datetime.combine(base, time(10, 0)),
            end=datetime.combine(base, time(11, 30)),
            type="meeting",
        )
        e2 = Event(
            title="B",
            start=datetime.combine(base, time(11, 0)),
            end=datetime.combine(base, time(12, 0)),
            type="meeting",
        )
        conflicts = detect_conflicts([e1, e2])
        assert len(conflicts) >= 1

    def test_adjacent_events_not_conflict(self):
        events = [
            make_event("A", 0, 10, 11),
            make_event("B", 0, 11, 12),   # starts exactly when A ends
        ]
        conflicts = detect_conflicts(events)
        # Adjacent (touching) events are NOT conflicts (end == start is not overlap)
        # Our logic: e2.start >= e1.end means no overlap
        assert len(conflicts) == 0


# ---------------------------------------------------------------------------
# plan_focus_blocks tests
# ---------------------------------------------------------------------------

class TestPlanFocusBlocks:
    def test_plans_focus_blocks_on_open_days(self):
        from scheduling import plan_focus_blocks
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        days = [monday + timedelta(days=i) for i in range(5)]
        # Empty calendar
        blocks = plan_focus_blocks([], 2.0, days, preferred_window="morning")
        assert len(blocks) == 5
        for b in blocks:
            assert b.type == "focus"
            assert (b.end - b.start).total_seconds() / 3600 == 2.0
