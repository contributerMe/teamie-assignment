"""
tests/test_manual_crud.py – Tests for manual CRUD operations and drag-and-drop support.
"""
from __future__ import annotations
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import datetime, date, timedelta, time
import pytest
from models import Event, Attendee
from calendar_store import MockCalendarStore
from agent import KairosAgent


def _this_monday() -> date:
    today = date.today()
    return today - timedelta(days=today.weekday())


class TestManualCrud:
    def setup_method(self):
        self.store = MockCalendarStore()
        self.agent = KairosAgent(self.store)
        self.monday = _this_monday()

    def test_manual_create_event_and_undo(self):
        start = datetime.combine(self.monday, time(13, 0))
        end = datetime.combine(self.monday, time(14, 0))
        new_event = Event(
            title="Custom Lunch Sync",
            start=start,
            end=end,
            type="meeting",
        )
        ok, msg = self.agent.manual_create_event(new_event)
        assert ok is True
        assert "Custom Lunch Sync" in msg

        # Verify event is in store
        events = self.store.list_events(start - timedelta(hours=1), end + timedelta(hours=1))
        assert any(e.id == new_event.id for e in events)

        # Test Undo
        ok_undo, undo_msg = self.agent.undo_last()
        assert ok_undo is True
        events_after = self.store.list_events(start - timedelta(hours=1), end + timedelta(hours=1))
        assert not any(e.id == new_event.id for e in events_after)

    def test_manual_update_move_event_and_undo(self):
        events = self.store.list_events(
            datetime.combine(self.monday, time(0, 0)),
            datetime.combine(self.monday + timedelta(days=5), time(23, 59))
        )
        target = events[0]
        orig_start = target.start
        orig_end = target.end

        new_start = datetime.combine(self.monday + timedelta(days=2), time(16, 0))
        new_end = datetime.combine(self.monday + timedelta(days=2), time(17, 0))

        ok, msg, updated = self.agent.manual_update_event(target.id, new_start, new_end)
        assert ok is True
        assert updated.start == new_start
        assert updated.end == new_end

        # Verify store updated
        ev_in_store = next(e for e in self.store.list_events(new_start, new_end) if e.id == target.id)
        assert ev_in_store.start == new_start

        # Undo move
        ok_undo, _ = self.agent.undo_last()
        assert ok_undo is True
        reverted = next(e for e in self.store.list_events(orig_start, orig_end) if e.id == target.id)
        assert reverted.start == orig_start

    def test_manual_delete_event_and_undo(self):
        events = self.store.list_events(
            datetime.combine(self.monday, time(0, 0)),
            datetime.combine(self.monday + timedelta(days=5), time(23, 59))
        )
        target = events[0]
        event_id = target.id

        ok, msg = self.agent.manual_delete_event(event_id)
        assert ok is True

        # Verify gone from store
        events_after = self.store.list_events(
            datetime.combine(self.monday, time(0, 0)),
            datetime.combine(self.monday + timedelta(days=5), time(23, 59))
        )
        assert not any(e.id == event_id for e in events_after)

        # Undo delete
        ok_undo, _ = self.agent.undo_last()
        assert ok_undo is True
        events_restored = self.store.list_events(
            datetime.combine(self.monday, time(0, 0)),
            datetime.combine(self.monday + timedelta(days=5), time(23, 59))
        )
        assert any(e.id == event_id for e in events_restored)

    def test_manual_rename_event_and_undo(self):
        events = self.store.list_events(
            datetime.combine(self.monday, time(0, 0)),
            datetime.combine(self.monday + timedelta(days=5), time(23, 59))
        )
        target = events[0]
        orig_title = target.title

        ok, msg, updated = self.agent.manual_update_event(target.id, title="Renamed Strategy Session")
        assert ok is True
        assert "Renamed" in msg
        assert updated.title == "Renamed Strategy Session"

        # Verify in store
        ev_in_store = next(e for e in self.store.list_events(target.start, target.end) if e.id == target.id)
        assert ev_in_store.title == "Renamed Strategy Session"

        # Undo rename
        ok_undo, undo_msg = self.agent.undo_last()
        assert ok_undo is True
        reverted = next(e for e in self.store.list_events(target.start, target.end) if e.id == target.id)
        assert reverted.title == orig_title

