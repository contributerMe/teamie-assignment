"""
agent.py – Gemini-powered agent with function calling using the google-genai SDK.
Falls back to rule-based offline mode if API key is missing or calls fail.
"""
from __future__ import annotations
import os
import re
import json
import logging
from datetime import datetime, timedelta, date, time
from typing import Optional, Any

from models import (
    Event, Change, Proposal, SlotOption, HealthReport, ChatMessage,
    Attendee, EventType
)
from scheduling import find_slots, plan_focus_blocks, health_score, detect_conflicts

logger = logging.getLogger(__name__)


def _console_log(msg: str) -> None:
    logger.info(msg)
    print(msg, flush=True)

MAX_ITERATIONS = 5

SYSTEM_PROMPT_TEMPLATE = """You are Kairos, an AI calendar assistant for business users. 
Today is {today}, {weekday}. The user's timezone is {timezone}. Working hours: 09:00–18:00 Mon–Fri.

Your job is to understand the user's scheduling intent and use the available tools to help them.

CRITICAL RULES:
1. NEVER claim an action has been done. Always propose and wait for user approval.
2. Every suggestion must include a "reasoning" field explaining WHY.
3. Use the tools to gather real data before making proposals.
4. Keep final replies short (1-3 sentences max). The UI shows the proposal and slot options separately.
5. When finding slots, always call find_meeting_slots so the system can present ranked options.
6. Write-type tools only return a proposal and never directly mutate the calendar.
7. If an attendee's calendar isn't visible, mention that their availability was unknown in the reasoning."""

TOOL_DEFINITIONS = [
    {
        "name": "list_events",
        "description": "List calendar events for a date range. Use this to understand what's on the calendar.",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "ISO date string YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "ISO date string YYYY-MM-DD"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_free_busy",
        "description": "Lookup free/busy intervals for given attendees within a date range.",
        "parameters": {
            "type": "object",
            "properties": {
                "attendees": {"type": "array", "items": {"type": "string"}, "description": "Attendee email addresses"},
                "start_date": {"type": "string", "description": "ISO date string YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "ISO date string YYYY-MM-DD"},
            },
            "required": ["attendees", "start_date", "end_date"],
        },
    },
    {
        "name": "get_calendar_health",
        "description": "Get the calendar health score, meeting load, focus time, and flagged issues for the current week.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "find_meeting_slots",
        "description": "Find ranked available time slots for a meeting with given attendees.",
        "parameters": {
            "type": "object",
            "properties": {
                "attendees": {"type": "array", "items": {"type": "string"},
                              "description": "List of attendee email addresses"},
                "duration_minutes": {"type": "integer", "description": "Meeting duration in minutes"},
                "before_date": {"type": "string", "description": "Find slots before this date YYYY-MM-DD"},
                "prefer_time": {"type": "string", "enum": ["morning", "afternoon", "any"],
                                "description": "Time preference"},
            },
            "required": ["duration_minutes"],
        },
    },
    {
        "name": "propose_focus_blocks",
        "description": "Propose focus/deep-work blocks for the week.",
        "parameters": {
            "type": "object",
            "properties": {
                "hours_per_day": {"type": "number", "description": "Hours of focus time per day"},
                "preferred_window": {"type": "string", "enum": ["morning", "afternoon"],
                                     "description": "Preferred time of day"},
                "days": {"type": "array", "items": {"type": "string"},
                         "description": "List of ISO date strings to add focus blocks"},
            },
            "required": ["hours_per_day"],
        },
    },
    {
        "name": "propose_reschedule",
        "description": "Propose moving an existing event to a different time.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "ID of the event to move"},
                "new_start": {"type": "string", "description": "New start datetime ISO format"},
                "new_end": {"type": "string", "description": "New end datetime ISO format"},
                "reason": {"type": "string", "description": "Why this reschedule is proposed"},
            },
            "required": ["event_id", "new_start", "new_end", "reason"],
        },
    },
    {
        "name": "propose_cancel",
        "description": "Propose cancelling an event.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "ID of the event to cancel"},
                "reason": {"type": "string", "description": "Why cancellation is proposed"},
            },
            "required": ["event_id", "reason"],
        },
    },
]


class KairosAgent:
    def __init__(self, store):
        self.store = store
        self._pending_proposals: dict[str, Proposal] = {}
        self._pending_slots: list[SlotOption] = []
        self._turn_proposal: Optional[Proposal] = None
        self._applied_stack: list[tuple[Proposal, list[Event]]] = []  # for undo

        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        configured_model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
        # Fallback chain: primary requested model -> gemini-4-hydrogen (secondary) -> gemini-3.8-flash
        models = [configured_model]
        for m in ["gemini-3.5-flash-lite", "gemini-4-hydrogen"]:
            if m not in models:
                models.append(m)
        self._models_to_try = models
        self._model_name = self._models_to_try[0]
        self._gemini_available = bool(api_key)
        self._client = None
        self._sdk_type = None

        if self._gemini_available:
            # 1. Try modern google-genai SDK first
            try:
                from google import genai
                self._client = genai.Client(api_key=api_key)
                self._sdk_type = "google-genai"
                logger.info(f"Gemini configured using google-genai SDK with model chain: {self._models_to_try}")
            except (ImportError, Exception) as e:
                # 2. Try legacy google-generativeai SDK as fallback
                try:
                    import google.generativeai as genai_legacy
                    genai_legacy.configure(api_key=api_key)
                    self._genai_legacy = genai_legacy
                    self._sdk_type = "google-generativeai"
                    logger.info(f"Gemini configured using google-generativeai SDK with model chain: {self._models_to_try}")
                except (ImportError, Exception) as err:
                    logger.warning(f"Neither google-genai nor google-generativeai could be loaded: {err}. Using offline mode.")
                    self._gemini_available = False

    def _week_range(self) -> tuple[datetime, datetime]:
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        start = datetime.combine(monday, time(0, 0))
        end = start + timedelta(days=7)
        return start, end

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _tool_list_events(self, start_date: str, end_date: str) -> dict:
        start = datetime.fromisoformat(start_date)
        end = datetime.fromisoformat(end_date)
        events = self.store.list_events(start, end)
        return {
            "events": [
                {
                    "id": e.id,
                    "title": e.title,
                    "start": e.start.isoformat(),
                    "end": e.end.isoformat(),
                    "type": e.type,
                    "attendees": [a.email for a in e.attendees],
                    "duration_minutes": e.duration_minutes,
                }
                for e in events
            ]
        }

    def _tool_get_free_busy(self, attendees: list[str], start_date: str, end_date: str) -> dict:
        start = datetime.fromisoformat(start_date)
        end = datetime.fromisoformat(end_date)
        fb = self.store.get_free_busy(attendees, start, end)
        return {
            email: [{"start": bs.isoformat(), "end": be.isoformat()} for bs, be in busy_list]
            for email, busy_list in fb.items()
        }

    def _tool_get_calendar_health(self) -> dict:
        start, end = self._week_range()
        events = self.store.list_events(start, end)
        report = health_score(events)
        return {
            "score": report.score,
            "meeting_hours": report.meeting_hours,
            "focus_hours": report.focus_hours,
            "back_to_back_count": report.back_to_back_count,
            "conflict_count": report.conflict_count,
            "issues": report.issues,
        }

    def _tool_find_meeting_slots(
        self,
        attendees: list[str] = None,
        duration_minutes: int = 30,
        before_date: str = None,
        prefer_time: str = "any",
    ) -> dict:
        attendees = attendees or []
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        earliest = datetime.combine(today, time(9, 0))
        if before_date:
            try:
                latest = datetime.fromisoformat(before_date)
            except Exception:
                latest = datetime.combine(monday + timedelta(days=5), time(18, 0))
        else:
            latest = datetime.combine(monday + timedelta(days=5), time(18, 0))

        start, end = self._week_range()
        events = self.store.list_events(start, end)
        attendee_busy = {}
        if attendees:
            attendee_busy = self.store.get_free_busy(attendees, start, end)

        prefer = None if prefer_time == "any" else prefer_time
        slots = find_slots(
            events=events,
            attendee_busy=attendee_busy,
            duration_min=duration_minutes,
            earliest=earliest,
            latest=latest,
            prefer=prefer,
            count=3,
        )

        unknown_attendees = [
            email for email, busy in attendee_busy.items()
            if not busy and email
        ]

        if unknown_attendees:
            note = f"Availability unknown for: {', '.join(unknown_attendees)}; slots proposed without their calendar."
            for s in slots:
                s.why = f"{s.why} ({note})"
        else:
            note = "All attendee calendars checked."

        self._pending_slots = slots

        return {
            "slots": [
                {
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat(),
                    "score": s.score,
                    "why": s.why,
                }
                for s in slots
            ],
            "unknown_attendees": unknown_attendees,
            "note": note,
        }

    def _tool_propose_focus_blocks(
        self,
        hours_per_day: float = 2.0,
        preferred_window: str = "morning",
        days: list[str] = None,
    ) -> dict:
        start, end = self._week_range()
        events = self.store.list_events(start, end)

        if days:
            day_dates = [date.fromisoformat(d) for d in days]
        else:
            today = date.today()
            monday = today - timedelta(days=today.weekday())
            # Search all 5 weekdays of the week
            day_dates = [monday + timedelta(days=i) for i in range(5)]

        proposed = plan_focus_blocks(events, hours_per_day, day_dates, preferred_window)
        if not proposed:
            return {
                "blocks_proposed": 0,
                "note": f"No available gaps found for {hours_per_day}h focus blocks on the requested days due to existing meetings."
            }

        changes = [Change(action="create", event=e) for e in proposed]

        proposal = Proposal(
            summary=f"Add {len(proposed)} focus block(s)",
            changes=changes,
            reasoning=(
                f"Scheduled {len(proposed)} focus block(s) in the {preferred_window} "
                f"on open days to protect deep work."
            ),
        )
        self._pending_proposals[proposal.id] = proposal
        self._turn_proposal = proposal
        return {"proposal_id": proposal.id, "blocks_proposed": len(proposed)}

    def _tool_propose_reschedule(
        self,
        event_id: str,
        new_start: str,
        new_end: str,
        reason: str,
    ) -> dict:
        start, end = self._week_range()
        events = self.store.list_events(start, end)
        event = next((e for e in events if e.id == event_id), None)
        if not event:
            return {"error": f"Event {event_id} not found"}

        updated = event.model_copy(update={
            "start": datetime.fromisoformat(new_start),
            "end": datetime.fromisoformat(new_end),
        })
        change = Change(action="update", event=updated, before=event)
        proposal = Proposal(
            summary=f"Move '{event.title}' to {new_start[:16]}",
            changes=[change],
            reasoning=reason,
        )
        self._pending_proposals[proposal.id] = proposal
        self._turn_proposal = proposal
        return {"proposal_id": proposal.id}

    def _tool_propose_cancel(self, event_id: str, reason: str) -> dict:
        start, end = self._week_range()
        events = self.store.list_events(start, end)
        event = next((e for e in events if e.id == event_id), None)
        if not event:
            return {"error": f"Event {event_id} not found"}

        change = Change(action="delete", event=event)
        proposal = Proposal(
            summary=f"Cancel '{event.title}'",
            changes=[change],
            reasoning=reason,
        )
        self._pending_proposals[proposal.id] = proposal
        self._turn_proposal = proposal
        return {"proposal_id": proposal.id}

    def _dispatch_tool(self, name: str, args: dict) -> Any:
        dispatch = {
            "list_events": self._tool_list_events,
            "get_free_busy": self._tool_get_free_busy,
            "get_calendar_health": self._tool_get_calendar_health,
            "find_meeting_slots": self._tool_find_meeting_slots,
            "propose_focus_blocks": self._tool_propose_focus_blocks,
            "propose_reschedule": self._tool_propose_reschedule,
            "propose_cancel": self._tool_propose_cancel,
        }
        fn = dispatch.get(name)
        if not fn:
            return {"error": f"Unknown tool: {name}"}
        try:
            return fn(**args)
        except Exception as e:
            logger.error(f"Tool {name} failed: {e}")
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Gemini Agent Execution Loop
    # ------------------------------------------------------------------

    def _gemini_chat(self, user_message: str) -> ChatMessage:
        self._pending_slots = []
        self._turn_proposal = None
        today = datetime.now()
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            today=today.strftime("%Y-%m-%d"),
            weekday=today.strftime("%A"),
            timezone="local",
        )

        errors = []
        for model_name in self._models_to_try:
            try:
                self._model_name = model_name
                if self._sdk_type == "google-genai":
                    res = self._chat_via_genai_sdk(user_message, system_prompt, model_name)
                else:
                    res = self._chat_via_generativeai_sdk(user_message, system_prompt, model_name)
                logger.info(f"Gemini call succeeded with model: {model_name}")
                return res
            except Exception as e:
                logger.warning(f"Gemini model '{model_name}' failed: {e}. Trying fallback model...")
                errors.append(f"{model_name}: {str(e)[:120]}")

        # If both/all models failed, only then fall back to offline mode
        logger.error(f"All Gemini models failed ({errors}). Falling back to offline mode.")
        return self._offline_chat(user_message, error="; ".join(errors))

    def _chat_via_genai_sdk(self, user_message: str, system_prompt: str, model_name: str) -> ChatMessage:
        from google.genai import types

        _console_log("━" * 60)
        _console_log(f"📨 [USER INPUT] '{user_message}'")
        _console_log(f"🤖 [LLM REQUEST] Model: {model_name}")

        tools = [{"function_declarations": TOOL_DEFINITIONS}]
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=tools,
            temperature=0.2,
        )

        chat = self._client.chats.create(model=model_name, config=config)
        response = chat.send_message(user_message)

        for iteration in range(MAX_ITERATIONS):
            fn_calls = []
            if hasattr(response, "function_calls") and response.function_calls:
                fn_calls = response.function_calls
            elif hasattr(response, "candidates") and response.candidates:
                for cand in response.candidates:
                    if hasattr(cand, "content") and cand.content and cand.content.parts:
                        for part in cand.content.parts:
                            if hasattr(part, "function_call") and part.function_call:
                                fn_calls.append(part.function_call)

            if not fn_calls:
                break

            tool_responses = []
            for fc in fn_calls:
                args = dict(fc.args) if hasattr(fc, "args") and fc.args else {}
                _console_log(f"⚡ [LLM TOOL CALL] {fc.name}")
                _console_log(f"   ↳ Arguments: {args}")
                result = self._dispatch_tool(fc.name, args)
                _console_log(f"   ↳ Result: {result}")
                tool_responses.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response={"result": result},
                    )
                )
            response = chat.send_message(tool_responses)

        reply_text = response.text or "I've reviewed your calendar and prepared suggestions."
        latest_proposal = self._turn_proposal

        _console_log(f"💬 [LLM REPLY] {reply_text}")
        if latest_proposal:
            _console_log(f"📋 [PROPOSAL] {latest_proposal.summary} ({len(latest_proposal.changes)} change(s))")
        else:
            _console_log("📋 [PROPOSAL] None generated this turn.")
        _console_log("━" * 60)

        return ChatMessage(
            role="assistant",
            content=reply_text,
            proposal=latest_proposal,
            slot_options=self._pending_slots if self._pending_slots else None,
            mode="gemini",
        )

    def _chat_via_generativeai_sdk(self, user_message: str, system_prompt: str, model_name: str) -> ChatMessage:
        from google.generativeai.types import FunctionDeclaration, Tool
        tools = Tool(function_declarations=[FunctionDeclaration(**t) for t in TOOL_DEFINITIONS])

        _console_log("━" * 60)
        _console_log(f"📨 [USER INPUT] '{user_message}'")
        _console_log(f"🤖 [LLM REQUEST] Model: {model_name}")

        model = self._genai_legacy.GenerativeModel(
            model_name=model_name,
            system_instruction=system_prompt,
            tools=[tools],
        )
        chat = model.start_chat()
        response = chat.send_message(user_message)

        for iteration in range(MAX_ITERATIONS):
            fn_calls = []
            for part in response.parts:
                if hasattr(part, "function_call") and part.function_call:
                    fn_calls.append(part.function_call)

            if not fn_calls:
                break

            tool_responses = []
            for fc in fn_calls:
                args = dict(fc.args) if hasattr(fc, "args") and fc.args else {}
                _console_log(f"⚡ [LLM TOOL CALL] {fc.name}")
                _console_log(f"   ↳ Arguments: {args}")
                result = self._dispatch_tool(fc.name, args)
                _console_log(f"   ↳ Result: {result}")
                tool_responses.append(
                    self._genai_legacy.protos.Part(
                        function_response=self._genai_legacy.protos.FunctionResponse(
                            name=fc.name,
                            response={"result": result},
                        )
                    )
                )
            response = chat.send_message(tool_responses)

        reply_text = ""
        for part in response.parts:
            if hasattr(part, "text") and part.text:
                reply_text += part.text

        if not reply_text:
            reply_text = "I've prepared a proposal for your calendar."

        latest_proposal = self._turn_proposal

        _console_log(f"💬 [LLM REPLY] {reply_text}")
        if latest_proposal:
            _console_log(f"📋 [PROPOSAL] {latest_proposal.summary} ({len(latest_proposal.changes)} change(s))")
        else:
            _console_log("📋 [PROPOSAL] None generated this turn.")
        _console_log("━" * 60)

        return ChatMessage(
            role="assistant",
            content=reply_text,
            proposal=latest_proposal,
            slot_options=self._pending_slots if self._pending_slots else None,
            mode="gemini",
        )

    # ------------------------------------------------------------------
    # Offline Rule-Based Fallback
    # ------------------------------------------------------------------

    def _offline_chat(self, user_message: str, error: str = "") -> ChatMessage:
        msg_lower = user_message.lower().strip()

        # (a) "how's my week" / "brief" -> health summary
        if any(kw in msg_lower for kw in ["how's my week", "how is my week", "brief", "health",
                                          "my week looking", "week look"]):
            return self._offline_health()

        # (b) "find N mins with X and Y before <day>" -> slot options
        m = re.search(r"find\s+(\d+)\s*min", msg_lower)
        if m:
            return self._offline_find_slots(user_message, int(m.group(1)))

        # (c) "block N hours focus time (every morning)" -> focus-block proposal
        m = re.search(r"block\s+([\d.]+)\s*hour", msg_lower)
        if m:
            return self._offline_focus_blocks(float(m.group(1)), user_message)

        # (d) "fix my <day>" -> reschedule/cancel proposal for lowest-value meetings
        for day_name in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
            if f"fix my {day_name}" in msg_lower or f"fix {day_name}" in msg_lower:
                return self._offline_fix_day(day_name)

        # (e) "move my <time> meeting to <day/time>" -> update proposal
        if "move" in msg_lower:
            return self._offline_move_meeting(user_message)

        prefix = "Offline mode" if not error else f"Offline fallback (Gemini error: {error[:40]})"
        return ChatMessage(
            role="assistant",
            content=(
                f"{prefix}: Try asking:\n"
                "• 'How's my week looking?'\n"
                "• 'Find 45 mins with Priya and Rahul before Friday'\n"
                "• 'Block 2 hours of focus time every morning this week'\n"
                "• 'Fix my Thursday'\n"
                "• 'Move my 3pm to tomorrow'"
            ),
            mode="offline",
        )

    def _offline_health(self) -> ChatMessage:
        start, end = self._week_range()
        events = self.store.list_events(start, end)
        report = health_score(events)

        summary_parts = [f"Your week scores **{report.score}/100**."]
        summary_parts.append(
            f"You have {report.meeting_hours}h of meetings and {report.focus_hours}h of focus time."
        )
        if report.back_to_back_count:
            summary_parts.append(f"⚠️ {report.back_to_back_count} back-to-back meeting(s).")
        if report.conflict_count:
            summary_parts.append(f"🔴 {report.conflict_count} scheduling conflict(s) detected.")
        if report.issues:
            summary_parts.append("Key issues: " + "; ".join(report.issues[:3]))

        return ChatMessage(
            role="assistant",
            content=" ".join(summary_parts),
            mode="offline",
        )

    def _offline_find_slots(self, user_message: str, duration: int) -> ChatMessage:
        attendees_map = {
            "priya": "priya@example.com",
            "rahul": "rahul@example.com",
            "ananya": "ananya@example.com",
            "vikram": "vikram@example.com",
        }
        msg_lower = user_message.lower()
        found_attendees = [email for name, email in attendees_map.items() if name in msg_lower]

        raw_emails = re.findall(r"[\w\.-]+@[\w\.-]+", user_message)
        for email in raw_emails:
            if email not in found_attendees:
                found_attendees.append(email)

        today = date.today()
        monday = today - timedelta(days=today.weekday())
        latest = datetime.combine(monday + timedelta(days=5), time(18, 0))
        for day_name, offset in [("friday", 4), ("thursday", 3), ("wednesday", 2),
                                 ("tuesday", 1), ("monday", 0)]:
            if day_name in msg_lower:
                latest = datetime.combine(monday + timedelta(days=offset), time(18, 0))
                break

        start, end = self._week_range()
        events = self.store.list_events(start, end)
        attendee_busy = self.store.get_free_busy(found_attendees, start, end) if found_attendees else {}

        slots = find_slots(
            events=events,
            attendee_busy=attendee_busy,
            duration_min=duration,
            earliest=datetime.combine(today, time(9, 0)),
            latest=latest,
            count=3,
        )

        who = ", ".join(found_attendees) or "attendees"
        if slots:
            msg = f"Found {len(slots)} ranked slot options for {duration} min with {who} before the deadline. Choose an option to generate a proposal."
        else:
            msg = f"No available slots found for {duration} min with {who} before the deadline."

        return ChatMessage(
            role="assistant",
            content=msg,
            slot_options=slots,
            mode="offline",
        )

    def _offline_focus_blocks(self, hours: float, user_message: str) -> ChatMessage:
        pref = "morning" if "morning" in user_message.lower() else "afternoon"
        result = self._tool_propose_focus_blocks(
            hours_per_day=hours,
            preferred_window=pref,
        )
        proposal_id = result.get("proposal_id")
        proposal = self._pending_proposals.get(proposal_id) if proposal_id else None
        n = result.get("blocks_proposed", 0)
        if n == 0 or not proposal:
            return ChatMessage(
                role="assistant",
                content=f"Could not fit {hours}h focus blocks in the {pref} because existing meetings fill the schedule. Try asking for shorter blocks (e.g. 1 hour) or afternoon focus time.",
                proposal=None,
                mode="offline",
            )
        return ChatMessage(
            role="assistant",
            content=f"Proposed {n} focus block(s) of {hours}h each ({pref} window). Review and click Approve to add them.",
            proposal=proposal,
            mode="offline",
        )

    def _offline_fix_day(self, day_name: str) -> ChatMessage:
        days_map = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4}
        offset = days_map.get(day_name.lower(), 3)
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        target = monday + timedelta(days=offset)

        start, end = self._week_range()
        events = self.store.list_events(start, end)
        day_events = [e for e in events if e.start.date() == target]

        if not day_events:
            return ChatMessage(
                role="assistant",
                content=f"No events found on {day_name.title()}.",
                mode="offline",
            )

        # 1. Resolve conflicts first
        conflicts = detect_conflicts(day_events)
        if conflicts:
            e1, e2 = conflicts[0]
            shorter = e1 if e1.duration_minutes <= e2.duration_minutes else e2
            longer = e2 if shorter == e1 else e1
            new_start = longer.end + timedelta(minutes=15)
            new_end = new_start + timedelta(minutes=shorter.duration_minutes)
            result = self._tool_propose_reschedule(
                shorter.id,
                new_start.isoformat(),
                new_end.isoformat(),
                f"'{shorter.title}' double-books with '{longer.title}'. Rescheduling it to clear the conflict."
            )
            proposal = self._pending_proposals.get(result.get("proposal_id", ""))
            return ChatMessage(
                role="assistant",
                content=f"Found double-booking on {day_name.title()}. Proposing to reschedule '{shorter.title}'.",
                proposal=proposal,
                mode="offline",
            )

        # 2. Cancel low-value meetings
        from scheduling import _is_low_value
        low_value = [e for e in day_events if _is_low_value(e)]
        if low_value:
            target_event = low_value[0]
            result = self._tool_propose_cancel(
                target_event.id,
                f"'{target_event.title}' is a large recurring status sync with no agenda. Cancelling saves {target_event.duration_minutes} mins."
            )
            proposal = self._pending_proposals.get(result.get("proposal_id", ""))
            return ChatMessage(
                role="assistant",
                content=f"Proposing to cancel low-value status meeting on {day_name.title()} to reduce meeting overload.",
                proposal=proposal,
                mode="offline",
            )

        # 3. If back-to-backs exist, propose moving one
        sorted_ev = sorted(day_events, key=lambda e: e.start)
        for i in range(len(sorted_ev) - 1):
            e1 = sorted_ev[i]
            e2 = sorted_ev[i + 1]
            gap = (e2.start - e1.end).total_seconds() / 60
            if 0 <= gap < 10:
                new_start = e1.end + timedelta(minutes=15)
                new_end = new_start + timedelta(minutes=e2.duration_minutes)
                result = self._tool_propose_reschedule(
                    e2.id,
                    new_start.isoformat(),
                    new_end.isoformat(),
                    f"Adding a 15-minute buffer between '{e1.title}' and '{e2.title}' to eliminate back-to-back fatigue."
                )
                proposal = self._pending_proposals.get(result.get("proposal_id", ""))
                return ChatMessage(
                    role="assistant",
                    content=f"Found back-to-back schedule on {day_name.title()}. Proposing to add a breathing buffer.",
                    proposal=proposal,
                    mode="offline",
                )

        return ChatMessage(
            role="assistant",
            content=f"{day_name.title()} is in good shape with {len(day_events)} well-spaced event(s).",
            mode="offline",
        )

    def _offline_move_meeting(self, user_message: str) -> ChatMessage:
        start, end = self._week_range()
        events = self.store.list_events(start, end)

        time_match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", user_message, re.I)
        target_hour = None
        if time_match:
            h = int(time_match.group(1))
            ampm = (time_match.group(3) or "").lower()
            if ampm == "pm" and h < 12:
                h += 12
            elif ampm == "am" and h == 12:
                h = 0
            elif not ampm and h <= 5:  # e.g., 3 -> 15:00
                h += 12
            target_hour = h

        candidate = None
        if target_hour is not None:
            for e in events:
                if e.start.hour == target_hour:
                    candidate = e
                    break

        if not candidate and events:
            candidate = next((e for e in events if e.type in ("meeting", "external")), events[0])

        if not candidate:
            return ChatMessage(
                role="assistant",
                content="Could not find a meeting matching your request.",
                mode="offline",
            )

        msg_lower = user_message.lower()
        days_map = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4}
        today = date.today()
        monday = today - timedelta(days=today.weekday())

        target_date = candidate.start.date() + timedelta(days=1)
        for day_name, offset in days_map.items():
            if day_name in msg_lower:
                target_date = monday + timedelta(days=offset)
                break

        new_start = datetime.combine(target_date, candidate.start.time())
        new_end = new_start + timedelta(minutes=candidate.duration_minutes)

        result = self._tool_propose_reschedule(
            candidate.id,
            new_start.isoformat(),
            new_end.isoformat(),
            f"Moved '{candidate.title}' to {new_start.strftime('%A, %b %d at %H:%M')} as requested.",
        )
        proposal = self._pending_proposals.get(result.get("proposal_id", ""))
        return ChatMessage(
            role="assistant",
            content=f"Prepared proposal to move '{candidate.title}' to {new_start.strftime('%A at %H:%M')}.",
            proposal=proposal,
            mode="offline",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chat(self, user_message: str) -> ChatMessage:
        if self._gemini_available:
            return self._gemini_chat(user_message)
        return self._offline_chat(user_message)

    def apply_proposal(self, proposal_id: str, custom_title: Optional[str] = None) -> tuple[bool, str]:
        proposal = self._pending_proposals.get(proposal_id)
        if not proposal:
            return False, "Proposal not found or already processed."
        if custom_title:
            for change in proposal.changes:
                if change.action in ("create", "update"):
                    change.event.title = custom_title
            proposal.summary = f"Schedule '{custom_title}'"
        applied = self.store.apply_changes(proposal.changes)
        self._applied_stack.append((proposal, applied))
        del self._pending_proposals[proposal_id]
        return True, f"Applied: {proposal.summary}"

    def reject_proposal(self, proposal_id: str) -> tuple[bool, str]:
        proposal = self._pending_proposals.pop(proposal_id, None)
        if not proposal:
            return False, "Proposal not found."
        return True, "Proposal rejected."

    def undo_last(self) -> tuple[bool, str]:
        if not self._applied_stack:
            return False, "Nothing to undo."
        proposal, _ = self._applied_stack.pop()
        reverse_changes = []
        for change in proposal.changes:
            if change.action == "create":
                reverse_changes.append(Change(action="delete", event=change.event))
            elif change.action == "delete":
                reverse_changes.append(Change(action="create", event=change.event))
            elif change.action == "update" and change.before:
                reverse_changes.append(Change(action="update", event=change.before, before=change.event))
        self.store.apply_changes(reverse_changes)
        return True, f"Undone: {proposal.summary}"

    def get_pending_proposal(self, proposal_id: str) -> Optional[Proposal]:
        return self._pending_proposals.get(proposal_id)

    def create_slot_proposal(
        self,
        slot: SlotOption,
        title: str,
        attendees: list[str],
        duration_min: int,
    ) -> Proposal:
        attendee_objs = [Attendee(email=e) for e in attendees]
        event = Event(
            title=title or "New Meeting",
            start=slot.start,
            end=slot.end,
            attendees=attendee_objs,
            type="meeting",
        )
        change = Change(action="create", event=event)
        proposal = Proposal(
            summary=f"Schedule '{event.title}' at {slot.start.strftime('%a %H:%M')}",
            changes=[change],
            reasoning=slot.why,
        )
        self._pending_proposals[proposal.id] = proposal
        return proposal

    def manual_create_event(self, event: Event) -> tuple[bool, str]:
        event.start = event.start.replace(tzinfo=None)
        event.end = event.end.replace(tzinfo=None)
        change = Change(action="create", event=event)
        applied = self.store.apply_changes([change])
        proposal = Proposal(
            summary=f"Add '{event.title}'",
            changes=[change],
            reasoning="Manually created by user",
        )
        self._applied_stack.append((proposal, applied))
        return True, f"Created '{event.title}'"

    def manual_update_event(
        self,
        event_id: str,
        new_start: Optional[datetime] = None,
        new_end: Optional[datetime] = None,
        title: Optional[str] = None,
        event_type: Optional[EventType] = None,
        description: Optional[str] = None,
    ) -> tuple[bool, str, Optional[Event]]:
        start, end = self._week_range()
        events = self.store.list_events(start, end)
        event = next((e for e in events if e.id == event_id), None)
        if not event:
            return False, f"Event {event_id} not found", None

        updates = {}
        if new_start is not None:
            updates["start"] = new_start.replace(tzinfo=None)
        if new_end is not None:
            updates["end"] = new_end.replace(tzinfo=None)
        if title is not None and title.strip():
            updates["title"] = title.strip()
        if event_type is not None:
            updates["type"] = event_type
        if description is not None:
            updates["description"] = description

        updated = event.model_copy(update=updates)
        change = Change(action="update", event=updated, before=event)
        applied = self.store.apply_changes([change])

        if title and title.strip() != event.title:
            summary = f"Renamed '{event.title}' to '{title.strip()}'"
            reason = "Manually renamed by user"
        elif new_start and new_start.replace(tzinfo=None) != event.start.replace(tzinfo=None):
            summary = f"Moved '{event.title}' to {new_start.strftime('%a %H:%M')}"
            reason = "Manually moved by user via drag-and-drop"
        else:
            summary = f"Updated '{event.title}'"
            reason = "Manually updated by user"

        proposal = Proposal(
            summary=summary,
            changes=[change],
            reasoning=reason,
        )
        self._applied_stack.append((proposal, applied))
        return True, summary, updated

    def manual_delete_event(self, event_id: str) -> tuple[bool, str]:
        start, end = self._week_range()
        events = self.store.list_events(start, end)
        event = next((e for e in events if e.id == event_id), None)
        if not event:
            return False, f"Event {event_id} not found"

        change = Change(action="delete", event=event)
        applied = self.store.apply_changes([change])
        proposal = Proposal(
            summary=f"Delete '{event.title}'",
            changes=[change],
            reasoning="Manually deleted by user",
        )
        self._applied_stack.append((proposal, applied))
        return True, f"Deleted '{event.title}'"
