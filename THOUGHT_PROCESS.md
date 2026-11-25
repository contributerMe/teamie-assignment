# Kairos – Design Document & Implementation Retrospective

---

## 1. Problem Statement and Goals

### The Problem
Modern calendar interfaces (Google Calendar, Microsoft Outlook, Apple Calendar) remain **passive visual grids**. They excel at displaying chronological blocks of time, but place the entire cognitive burden of calendar management on the user:
1. **Manual Negotiation**: Finding mutually agreeable meeting times across 3–4 busy colleagues requires tedious mental arithmetic, checking free/busy blocks, and back-and-forth messaging.
2. **Double-Booking & Conflict Fatigue**: Complex, dynamic workdays lead to accidental double-bookings or grueling strings of back-to-back meetings without bio or cognitive recovery breaks.
3. **Erosion of Deep Work**: Fragmented 15–30 minute gaps scattered throughout a day make sustained focus impossible, destroying engineering and strategic productivity.

### The Insight: "Propose, Don't Act"
The calendar is not fundamentally a display problem; it is a **time optimization problem**. Business users need an **agent**, not another viewer. 

However, fully autonomous calendar agents suffer from a fatal **trust barrier**: if an AI automatically reschedules a meeting or cancels an appointment without explicit consent, user trust is destroyed permanently.

### Core Goals
- **Propose, Don't Act**: The agent only generates structured proposals. No database mutation or API write occurs until the user inspects the visual diff and clicks **✓ Approve**.
- **Transparent "Why" Rationales**: Every suggested slot, reschedule, or focus block includes a deterministic reason (e.g., *"Midday slot avoids breaking morning deep work; preserves 15-min breathing room"*).
- **Zero-Hallucination Scheduling**: All mathematical scheduling logic (interval subtraction, gap scoring, conflict detection) is executed in deterministic Python. The LLM is restricted to semantic intent extraction and conversational phrasing.
- **Offline & Fallback Resilience**: The system must run completely without external credentials (rule-based offline fallback) and gracefully handle model deprecations.

---

## 2. Initial Plan: The "All-in-One-Pass" Mistake

The initial development strategy was ambitious: **build all layers concurrently in a single monolithic pass**.
- Simultaneously draft Pydantic domain models (`models.py`), the deterministic scheduling math (`scheduling.py`), the multi-turn Gemini agent loop (`agent.py`), the Google OAuth & mock stores (`calendar_store.py`), the FastAPI routes (`main.py`), and a complex single-page Tailwind UI (`static/index.html`).

### The Decision
We assumed that since the domain models were relatively clean, modern AI code generation could synthesize the entire full-stack application end-to-end, allowing us to test everything together right away.

---

## 3. What Went Wrong and What We Learned (Retrospective)

### The Shortcomings of the Initial Approach
Building everything at once was **the wrong architectural and testing decision**. 

When the complete stack was launched simultaneously:
1. **Compounding Bugs**: Failures in the frontend JavaScript masked backend exceptions, while LLM prompt edge cases made deterministic functions look buggy.
2. **Hidden Silent Failures**: When a slot was not proposed, it was initially impossible to tell whether:
   - Gemini failed to call the function declaration,
   - The scheduling engine found zero valid intervals,
   - The mock calendar store filtered out events, or
   - The browser UI failed to render the response.
3. **Process & Tool Deadlocks**: Attempting to run debugging harnesses with `--reload` under remote debuggers caused parent-child process deadlocks over open sockets.

### Hard Lessons Learned
- **Never couple LLM agent validation with UI rendering**: The deterministic scheduling engine and LLM function calling loop should have been built, isolated, and unit-tested with rigorous assertions *first* before a single line of HTML or CSS was written.
- **AI-generated code requires strict validation boundaries**: LLMs produce subtly flawed code when handling edge cases like timezone-aware vs. timezone-naive datetime comparisons, SDK deprecation fallbacks, and shared mutable state across conversation turns.

---

## 4. Revised Implementation Plan

Once we acknowledged the failure of the all-in-one-pass approach, we pivoted to a disciplined, phased execution strategy:

```
┌────────────────────────────────────────────────────────┐
│  Phase 1: Deterministic Core & Agent Isolation         │
│  - Pure Python scheduling algorithms (scheduling.py)  │
│  - Pytest test suite for interval math & scoring       │
│  - Structured stdout logging for LLM function calls    │
└──────────────────────────┬─────────────────────────────┘
                           │ verified with unit tests
                           ▼
┌────────────────────────────────────────────────────────┐
│  Phase 2: Store Abstraction & Integration Isolation    │
│  - MockCalendarStore with deterministic seed week      │
│  - GoogleCalendarStore with Desktop OAuth2 flow        │
│  - Idempotent test calendar seeder (seed_google.py)    │
└──────────────────────────┬─────────────────────────────┘
                           │ verified with curl & mock harness
                           ▼
┌────────────────────────────────────────────────────────┐
│  Phase 3: Visual Interface & Full E2E Integration     │
│  - Week calendar grid with CSS grid & time line        │
│  - Ghost block visual diffing for proposals            │
│  - HTML5 Drag-and-drop & inline modal editing          │
│  - End-to-end conversational workflow verification     │
└────────────────────────────────────────────────────────┘
```

---

## 5. Architectural Decision Records (ADRs)

### ADR 1: FastAPI for Backend Service
- **Context**: Need a fast, typed Python web service capable of handling asynchronous API requests, serving single-page static assets, and providing native Pydantic schema serialization.
- **Options Considered**:
  1. *Flask*: Simple, but synchronous by default and requires third-party extensions for Pydantic and OpenAPI validation.
  2. *Django*: Heavyweight with unnecessary ORM and admin overhead for a prototype.
  3. *FastAPI*: Native ASGI asynchronous performance, automatic Pydantic request/response validation, and minimal boilerplate.
- **Decision**: **FastAPI**.
- **Consequences**: Rapid endpoint creation, clear error responses on malformed JSON, and seamless OpenAPI schema documentation.

---

### ADR 2: LLM Selection & Cascading Fallback Chain
- **Context**: The application relies on function calling to map conversational intent to backend tools. During testing, the hardcoded `gemini-2.0-flash` endpoint returned `404 NOT_FOUND` due to regional API deprecation on the user's key.
- **Options Considered**:
  1. *Single hardcoded model*: Fragile; any API quota, permission, or deprecation breaks the whole application.
  2. *External orchestration framework (LangChain/LlamaIndex)*: High dependency overhead, unpredictable prompt changes, and poor visibility into tool argument JSON.
  3. *Native Google GenAI SDK with cascading fallbacks*: Direct API client with multi-model cascade and local rule-based fallback.
- **Decision**: **Native Google GenAI SDK with cascading fallbacks**.
  - Cascading chain: `gemini-3.5-flash-lite` $\rightarrow$ `gemini-4-hydrogen` $\rightarrow$ `gemini-3.8-flash` $\rightarrow$ offline rule-based fallback.
- **Consequences**: If the primary model fails or hits rate limits, the agent automatically retries with secondary and tertiary models before degrading gracefully to offline regex rules. Zero external framework bloat.

---

### ADR 3: Desktop OAuth vs. Service Account for Google Calendar
- **Context**: Kairos must read user events, query free/busy intervals, and insert calendar blocks into real Google accounts.
- **Options Considered**:
  1. *Google Service Account*: Cannot access human user calendars without Google Workspace Domain-Wide Delegation (unavailable for personal `@gmail.com` accounts).
  2. *OAuth 2.0 Web Server Flow*: Requires hosting an external redirect URI with HTTPS certificates.
  3. *OAuth 2.0 Installed / Desktop App Flow (`InstalledAppFlow`)*: Runs a local loopback server (`http://localhost:8000/auth/google`), allowing any human user to sign in with their own browser.
- **Decision**: **OAuth 2.0 Desktop App Flow**.
- **Consequences**: Seamless local developer experience; `token.json` is safely cached locally and refreshed automatically.

---

### ADR 4: In-Memory Storage & Pluggable Architecture vs. Immediate Database
- **Context**: Rapid prototyping requires instant execution without requiring developers to spin up PostgreSQL containers before testing UI flows.
- **Options Considered**:
  1. *SQLite*: Lightweight, but complicates in-memory test resets and demo calendar seeding.
  2. *PostgreSQL from Day 1*: Added Docker and migration overhead before core interaction loops were verified.
  3. *Abstract `CalendarStore` ABC*: Pluggable interface with in-memory `MockCalendarStore` for instant zero-dependency testing, and `GoogleCalendarStore` for real accounts.
- **Decision**: **Abstract `CalendarStore` interface**.
- **Consequences**: Kairos runs out-of-the-box with zero setup (`python -m uvicorn main:app`). A production PostgreSQL implementation can now be added as `PostgresCalendarStore(CalendarStore)` without altering a single line of agent or scheduling code.

---

### ADR 5: Deterministic Python vs. LLM for Slot Calculation
- **Context**: Calculating open slots requires subtracting busy attendee intervals, reserving 60-minute lunch breaks, enforcing 10-minute buffers, and respecting 9:00–18:00 boundaries.
- **Options Considered**:
  1. *Prompt Engineering (asking LLM to calculate slots)*: LLMs consistently fail at modular interval arithmetic, hallucinate availability in occupied windows, or schedule meetings across midnight.
  2. *Pure Deterministic Python*: Interval subtraction using sorted `datetime` tuples and explicit scoring equations.
- **Decision**: **Deterministic Python in `scheduling.py`**.
- **Consequences**: 100% mathematical guarantee against scheduling double-bookings; zero hallucination risk. The LLM is strictly used to select tool arguments and craft conversational explanations.

---

## 6. Debugging and Testing Evidence: Concrete Bug Post-Mortems

Throughout development, several complex bugs were uncovered through manual testing, WSL logs, and remote debugger sessions:

### Bug 1: WSL Process Deadlocks with Uvicorn `--reload`
- **Symptom**: Launching `python -m debugpy --listen 0.0.0.0:5678 -m uvicorn main:app --reload` froze permanently upon the first request; VS Code attached but could not hit breakpoints.
- **Root Cause**: Uvicorn's `--reload` uses `multiprocessing.Process` to spawn a child watcher process. The child process attempted to re-bind to debugpy's port `5678`, causing a silent port collision and deadlock.
- **Fix**: Removed `--reload` when running with `debugpy` in `run-wid-debug`:
  ```bash
  python -m debugpy --listen 0.0.0.0:5678 -m uvicorn main:app --host 0.0.0.0 --port 8000
  ```

---

### Bug 2: Focus Block Window Bleeding into Afternoon
- **Symptom**: When a user asked *"Block 2 hours of focus time every morning this week"*, the assistant returned:
  ```text
  ➕ Add: Focus Block (1.5h) · Thu, 4:05 PM – 5:35 PM
  ➕ Add: Focus Block (1.5h) · Fri, 2:00 PM – 3:30 PM
  ```
- **Root Cause**: In `scheduling.py`, `plan_focus_blocks` defined `search_windows = [(9, 13), (14, 18)]`. When Thursday morning was packed with meetings, the algorithm silently fell back to the afternoon window, violating user intent.
- **Fix**: Enforced strict boundary conditions:
  ```python
  if preferred_window == "morning":
      search_windows = [(9, 13)]
  elif preferred_window == "afternoon":
      search_windows = [(14, 18)]
  ```
  If no morning slot fits, it honestly returns `blocks_proposed: 0` with an explanatory note rather than placing unwanted afternoon blocks.

---

### Bug 3: Stale Proposal Leakage Across Conversation Turns
- **Symptom**: If the user ran a prompt that produced zero blocks (or just asked a question), the previous turn's proposal card would reappear in the chat UI.
- **Root Cause**: `KairosAgent._pending_proposals` accumulated proposals in a persistent dictionary. When a turn produced no new proposal, the response builder fell back to `list(self._pending_proposals.values())[-1]`, attaching the old proposal from the previous turn.
- **Fix**: Introduced per-turn proposal isolation with `self._turn_proposal: Optional[Proposal] = None`. At the start of each chat invocation, `self._turn_proposal` is initialized to `None` and only populated if a tool explicitly creates a new proposal during that specific turn.

---

### Bug 4: Offset-Aware vs. Offset-Naive Datetime Comparison Crash
- **Symptom**: Submitting a manual meeting via the modal or drag-and-drop caused a `500 Internal Server Error` in FastAPI:
  ```text
  TypeError: can't compare offset-naive and offset-aware datetimes
  ```
- **Root Cause**: The frontend sent timestamps formatted with JavaScript's `.toISOString()`, which produces UTC strings with `Z` (e.g., `2026-11-23T04:30:00.000Z`). Python's `datetime.fromisoformat()` parsed this as timezone-aware, whereas seed events in `MockCalendarStore` were timezone-naive. When `detect_conflicts()` evaluated `e2.start >= e1.end`, Python threw a `TypeError`.
- **Fix**:
  1. Created `formatLocalISO(d)` in `static/index.html` to serialize wall-clock time without UTC translation.
  2. Implemented `_parse_dt()` in `main.py` and `.replace(tzinfo=None)` across `scheduling.py` and `agent.py` to ensure all internal datetime arithmetic remains strictly offset-naive.

---

## 7. AI Usage Disclosure

In compliance with engineering best practices, here is an explicit disclosure of how generative AI was utilized during this project:

| Component | AI Contribution | Human Verification & Modification |
|---|---|---|
| **Pydantic Domain Models** | Initial schema boilerplate generated by AI. | Hand-tuned duration properties, custom constructors, and JSON serialization encoders. |
| **Scheduling Algorithm** | Baseline interval subtraction logic generated by AI. | Completely rewritten: fixed boundary leaks, added strict morning/afternoon window isolation, and eliminated fragmentation bugs. |
| **Agent Tool Loop** | Initial Gemini function calling template generated by AI. | Added cascading multi-model fallback chain, per-turn proposal tracking, and rich terminal execution logging. |
| **Frontend UI** | HTML/Tailwind skeleton generated by AI. | Fixed JavaScript syntax errors, implemented HTML5 Drag-and-Drop snapping, created inline title edit handlers, and added the custom time line indicator. |
| **Google Calendar OAuth** | Initial OAuth client snippet suggested by AI. | Refactored into a clean class hierarchy with token auto-refresh and an idempotent test calendar seeder that refuses to touch `primary`. |

---

## 8. Limitations & Future Roadmap

### Current Limitations
1. **Volatile In-Memory State**: Proposals and mock calendar events do not survive server restarts.
2. **Fixed Work Windows**: Working hours (`09:00–18:00`) and lunch (`13:00–14:00`) are currently system constants.
3. **Single-User Scope**: Optimized for one executive user; does not yet display side-by-side multi-calendar columns.

### Proposed PostgreSQL Migration Schema
To transition Kairos from a prototype to a multi-tenant production SaaS, we have designed the following relational schema:

```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email VARCHAR(255) UNIQUE NOT NULL,
    full_name VARCHAR(255),
    timezone VARCHAR(64) DEFAULT 'UTC',
    work_start_time TIME DEFAULT '09:00:00',
    work_end_time TIME DEFAULT '18:00:00',
    lunch_start_time TIME DEFAULT '13:00:00',
    lunch_end_time TIME DEFAULT '14:00:00',
    allow_weekend_scheduling BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TYPE event_type_enum AS ENUM ('meeting', 'external', 'focus', 'personal', 'other');

CREATE TABLE events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    start_time TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    end_time TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    event_type event_type_enum NOT NULL DEFAULT 'meeting',
    description TEXT,
    location VARCHAR(255),
    organizer_email VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    CONSTRAINT chk_end_after_start CHECK (end_time > start_time)
);
CREATE INDEX idx_events_user_time ON events (user_id, start_time, end_time);

CREATE TABLE proposals (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    summary VARCHAR(255) NOT NULL,
    reasoning TEXT NOT NULL,
    status VARCHAR(32) DEFAULT 'pending', -- 'pending', 'applied', 'rejected', 'undone'
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE proposal_changes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    proposal_id UUID REFERENCES proposals(id) ON DELETE CASCADE,
    action VARCHAR(16) NOT NULL, -- 'create', 'update', 'delete'
    event_id UUID REFERENCES events(id) ON DELETE SET NULL,
    before_state JSONB,
    after_state JSONB
);
```

### Next Milestones
- **User Preference Modal**: Add a settings interface allowing users to adjust their personal working hours, lunch breaks, and weekend availability.
- **Multi-Week Navigation**: Implement previous/next week pagination and date-range pickers.
- **Google Push Webhooks**: Implement Google Cloud Pub/Sub push notifications to reflect external calendar edits in real time.
