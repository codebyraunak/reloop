# ReLoop / EcoRoute — E-Waste & Recycling Pickup Coordinator

ReLoop is an evidence-based, agentic e-waste and recycling pickup coordination platform. It integrates a bounded Gemini-powered AI agent with strict backend boundary validation, live mock recycler web scraping, human approval gating for fee exceptions, idempotent booking, and independent verification.

> **Integrity & Demo Safety Notice:**
> All recycler profiles, fees, source pages, and booking confirmations are synthetic mock entities designed for rigorous testing and demonstration.
> **No physical collection is arranged or claimed.** In accordance with strict safety requirements, the status `collected` does **not** exist anywhere in this system. The verified state is displayed as: **“Requested, not yet collected.”**

---

## Table of Contents
1. [Core Features](#core-features)
2. [System Architecture](#system-architecture)
3. [Booking Lifecycle State Machine](#booking-lifecycle-state-machine)
4. [Component Architecture](#component-architecture)
5. [Installation & Local Setup](#installation--local-setup)
6. [Environment Variables](#environment-variables)
7. [API Reference](#api-reference)
8. [Automated Testing & Benchmark Runner](#automated-testing--benchmark-runner)
9. [Official Hackathon Test Cases (TC1–TC4) Evidence](#official-hackathon-test-cases-tc1tc4-evidence)
10. [Deployment Guide (Vercel & Render)](#deployment-guide-vercel--render)
11. [Known Limitations](#known-limitations)
12. [2–3 Minute Demo Video Script](#23-minute-demo-video-script)
13. [External Submission Checklist](#external-submission-checklist)

---

## 1. Core Features

- **Strict Backend Boundary Validation:** Validates item categories, quantities ($\ge 1$), city, 6-digit Indian PIN code (`^[1-9][0-9]{5}$`), and non-negative fee limits before agent execution.
- **Natural Language Intent Extraction:** Distinguishes fee limits from item quantities (e.g., *"budget is ₹100 for 1 laptop"* correctly extracts quantity 1 and budget ₹100, never 100 laptops).
- **Evidence-Based Recycler Web Scraping:** Scrapes live semantic HTML pages for 4 distinct mock recyclers using `httpx` and `BeautifulSoup`, extracting accepted/rejected materials, service areas, and fee rates with source URLs and UTC timestamps.
- **Bounded Agent Workflow (Capped at 6 Steps):** Sequential, auditable tool loop that logs every step (`{step, tool, input, output, evidence, timestamp}`) displayed in real time in the UI trace panel.
- **Human Approval & Fee Exception Gating:** Requests requiring fee exceptions (where lowest matching fee exceeds the user's budget) transition to `awaiting_approval`. Bookings are hard-blocked (HTTP 409) until explicit approval of an immutable snapshot is recorded.
- **Idempotent Booking:** Retried bookings with the same request ID return the canonical booking reference without creating duplicate records.
- **Independent Verification:** A distinct verification engine fetches the live mock source evidence again, confirming agreement between stored booking state, approved payload, and live recycler parameters.
- **Database Reliability & Test Isolation:** Connects to Supabase PostgreSQL when `DATABASE_URL` is set, with automatic, graceful fallback to SQLite. Automated tests execute in an isolated SQLite environment (`TEST_MODE=1`) without touching external APIs or production databases.
- **Dedicated Benchmark `/tests` UI:** Standalone interactive test suite executing TC1–TC4 with live backend persistence in the `test_runs` table.

---

## 2. System Architecture

```mermaid
flowchart TD
    subgraph Client [Frontend Layer]
        UI[ReLoop Standalone UI - index.html]
        TracePanel[Live Agent Trace Panel]
        Modal[Human Approval Modal]
        TestRunner[Dedicated Benchmark Page - /tests]
    end

    subgraph Backend [FastAPI Application Core]
        API[API Endpoints /api/...]
        BoundaryVal[Boundary Input Validator]
        AgentCoord[Bounded Agent Coordinator - Max 6 Steps]
        Scraper[Live Recycler HTML Crawler & BS4 Parser]
        LifecycleEngine[Lifecycle & Idempotency Safeguards]
        Verifier[Independent Verification Engine]
    end

    subgraph External [External Services & Mock Ecosystem]
        MockPages[Mock Recycler Endpoints /mock-recycler/:slug]
        Gemini[Google Gemini API / Fallback Logic]
        Supabase[(Supabase PostgreSQL)]
        LocalDB[(SQLite Isolated / Fallback DB)]
    end

    UI -->|POST /api/requests| API
    UI -->|POST /api/requests/:id/approval| API
    UI -->|POST /api/requests/:id/book| API
    UI -->|POST /api/requests/:id/verify| API
    TestRunner -->|POST /api/tests/run| API

    API --> BoundaryVal
    BoundaryVal --> AgentCoord
    AgentCoord <-->|Optional Guidance| Gemini
    AgentCoord --> Scraper
    Scraper -->|HTTP GET & Parse| MockPages
    AgentCoord --> LifecycleEngine

    LifecycleEngine -->|psycopg / sqlite3| Supabase
    LifecycleEngine -.->|Fallback / Test Mode| LocalDB

    LifecycleEngine --> Verifier
    Verifier --> Scraper

    AgentCoord -->|Structured Trace| TracePanel
    LifecycleEngine -->|Approval Snapshot| Modal
```

---

## 3. Booking Lifecycle State Machine

The system strictly enforces the sequential state lifecycle. State jumps, booking without approval, or attempts to modify approved payloads are rejected with HTTP 409.

```mermaid
stateDiagram-v2
    [*] --> draft : Submit valid request
    draft --> awaiting_approval : Fee exceeds user limit
    draft --> approved : Auto-approved (within budget)
    awaiting_approval --> approved : Explicit Human Approval Modal
    awaiting_approval --> failed : User rejects fee exception

    approved --> requested : Idempotent book_pickup()
    requested --> verified : Independent verification passes
    requested --> unresolved : Missing/divergent evidence
    requested --> failed : Critical mismatch

    verified --> [*]
    unresolved --> [*]
    failed --> [*]

    note right of verified
      Status display:
      "Requested, not yet collected."
      No physical collection is arranged.
    end note
```

---

## 4. Component Architecture

### Frontend (`ecoroute/backend/static/index.html` & `tests.html`)
- Built with vanilla HTML, modern CSS tokens, and JavaScript.
- Retains authentic ReLoop green and off-white aesthetics, micro-animations, and clean typography.
- **Trace Panel:** Renders ordered step badges, tool names, inputs, outputs, and clickable source evidence links.
- **Approval Modal:** Accessible, styled dialog displaying snapshot items, quantities, chosen centre, fee discrepancy, and explicit confirm/cancel buttons.
- **Honest Status Badging:** Displays `"Requested, not yet collected."` with amber/green badge styles.
- **Benchmark Suite (`/tests`):** Provides individual run buttons for TC1–TC4, formatted JSON input/output cards, expected vs. actual results, and live pass/fail badges.

### Backend (`ecoroute/backend/`)
- `main.py`: FastAPI endpoints, CORS, static file mounts, boundary validation, approval authorization, idempotency guards, and test execution routes.
- `agent.py`: Bounded 6-step agent orchestrator. Coordinates intent extraction, mock profile retrieval, multi-item decision-making, fee comparisons, and trace logging.
- `mock_recyclers.py`: Defines 4 seeded recycler profiles, serves semantic HTML at `/mock-recycler/{slug}`, and crawls/parses pages with `httpx` and `BeautifulSoup`.
- `db.py`: Database abstraction supporting Supabase PostgreSQL via `psycopg` and SQLite via `sqlite3`. Manages unified dictionary cursor wrappers, non-destructive schema migrations, test isolation, and status sanitization.

### Mock Recycler Profiles
1. **GreenLoop Electronics Recovery (`mock-recycler-a` / `/mock-recycler/greenloop`):** Accepts laptops, mobile phones, desktops, cables. Rejects batteries, paper, appliances. Fee: ₹50.
2. **VoltCycle Battery & Gadget Hub (`mock-recycler-b` / `/mock-recycler/voltcycle`):** Accepts batteries, mobile phones, tablets, cameras. Rejects paper, CRT TVs, refrigerators. Fee: ₹180.
3. **PaperBack Materials Centre (`mock-recycler-c` / `/mock-recycler/paperback`):** Accepts paper, cardboard pulp. Rejects all electronics, batteries, laptops, mobile phones. Fee: ₹20.
4. **Apex EcoCollect Special Services (`mock-recycler-d` / `/mock-recycler/apex`):** Accepts all e-waste items and batteries across Karnataka. Rejects hazardous chemicals and bio-waste. Premium fee: ₹350 (ideal for TC3 fee limit tests).

---

## 5. Installation & Local Setup

### Prerequisites
- Python 3.11+
- Git

### Quickstart (Windows PowerShell)

```powershell
# Clone the repository
git clone https://github.com/codebyraunak/reloop.git
cd reloop

# Create and activate virtual environment
python -m venv .venv
.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Start the application server
uvicorn ecoroute.backend.main:app --reload --port 8000
```

### Quickstart (Linux / macOS)

```bash
git clone https://github.com/codebyraunak/reloop.git
cd reloop

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
uvicorn ecoroute.backend.main:app --reload --port 8000
```

Access the application:
- Main Consumer UI: [http://localhost:8000](http://localhost:8000)
- Benchmark Test Runner: [http://localhost:8000/tests](http://localhost:8000/tests)
- Interactive API Docs: [http://localhost:8000/docs](http://localhost:8000/docs)
- Health Check: [http://localhost:8000/api/health](http://localhost:8000/api/health)

---

## 6. Environment Variables

Create a `.env` file in the project root or configure via your hosting provider. See `.env.example`:

```ini
# Gemini API Configuration (Optional: falls back to deterministic coordinator if omitted)
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-flash-lite-latest

# Database Configuration (Supabase PostgreSQL or fallback to SQLite)
DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DATABASE

# Public Base URL for evidence source links (eliminates broken localhost links in production)
PUBLIC_BASE_URL=https://reloop.vercel.app

# CORS Configuration
CORS_ORIGINS=*

# Server Port
PORT=8000
```

> **Security Guarantee:** Environment secrets (`GEMINI_API_KEY`, `DATABASE_URL`) are strictly held on the server side and never committed, printed to console logs, or leaked to API responses.

---

## 7. API Reference

| Method | Path | Description | Safeguard |
|---|---|---|---|
| `GET` | `/api/health` | Health status and engine readiness | Returns DB dialect and Gemini connectivity state |
| `GET` | `/api/materials` | Supported item categories | Master enum of valid materials |
| `GET` | `/api/recyclers` | List of seeded recycler profiles | Provides service areas and material policies |
| `GET` | `/mock-recycler/{slug}` | Live semantic HTML profile | Scraped by `httpx` + `BeautifulSoup` |
| `POST` | `/api/assistant` | Natural language pickup parser | Separates budget from quantity |
| `POST` | `/api/requests` | Create pickup request draft | Validates PIN code, city, quantity, item types |
| `GET` | `/api/requests/{id}` | Retrieve request details & plan | Returns per-item decisions and trace |
| `POST` | `/api/requests/{id}/approval` | Record human approval | Stores immutable snapshot of approved payload |
| `POST` | `/api/requests/{id}/book` | Finalize pickup booking | Enforces approval, blocks drafts, idempotent |
| `POST` | `/api/requests/{id}/verify` | Independent booking verification | Re-evaluates live source evidence |
| `POST` | `/api/tests/run` | Execute all 4 benchmark cases | Persists run results to database |
| `POST` | `/api/tests/run/{case_id}` | Execute specific scenario (TC1–TC4) | Returns granular test evidence |
| `GET` | `/api/tests` | Retrieve test run history | Audit trail of benchmark executions |

---

## 8. Automated Testing & Benchmark Runner

### Running Unit & Integration Tests (pytest)
Automated tests execute against an isolated SQLite test database without contacting external APIs:

```bash
python -m pytest
```

**Test Suite Coverage (20 Passing Tests):**
1. `test_tc1_laptop_accepted_by_centre`: Draft creation with verified mock recycler acceptance.
2. `test_tc2_paper_accepted_batteries_rejected`: Multi-item decision verification (PaperBack accepts paper, rejects batteries).
3. `test_tc3_fee_above_limit_requires_approval`: Fee exception gating and HTTP 409 block on booking.
4. `test_tc4_pickup_requested_and_verified_not_collected`: End-to-end lifecycle verification with honest status.
5. `test_boundary_validation_invalid_pincode`: Rejection of non-6-digit PIN codes.
6. `test_boundary_validation_missing_city`: Rejection of empty city field.
7. `test_boundary_validation_zero_quantity`: Rejection of quantity $< 1$.
8. `test_boundary_validation_negative_fee`: Rejection of negative budgets.
9. `test_boundary_validation_unsupported_item`: Rejection of invalid item types.
10. `test_multi_item_request_independent_decisions`: Independent evaluation of multi-item requests.
11. `test_booking_draft_without_approval_blocked`: Rejection of unapproved draft bookings.
12. `test_booking_idempotency_returns_same_reference`: Duplicate booking requests yield identical references.
13. `test_approval_snapshot_immutability`: Post-approval tampering attempts are blocked.
14. `test_verification_independent_evidence_check`: Verification checks live mock source evidence.
15. `test_verification_fails_if_request_not_booked`: Rejection of unbooked verification requests.
16. `test_never_reports_or_exposes_collected_status`: Guarantees absence of `collected` status.
17. `test_agent_trace_schema_and_ordering`: Validates trace record structure and sequential timestamps.
18. `test_assistant_distinguishes_budget_from_quantity`: Intent parsing verification.
19. `test_tests_api_persists_results`: Persistence of test outcomes in `test_runs` table.
20. `test_mock_recycler_html_evidence_scraping`: Verification of `httpx` + `BeautifulSoup` live scraping.

---

## 9. Official Hackathon Test Cases (TC1–TC4) Evidence

Below are the actual results and evidence captured directly from the automated benchmark execution:

### TC1: Old Laptop Accepted by Centre
- **Input:** `{"item_type": "laptop", "quantity": 1, "city": "Mysuru", "pincode": "570001", "max_fee": 100.0}`
- **Expected:** Pickup draft created with evidenced recycler acceptance.
- **Actual:** Pickup draft created; selected centre: GreenLoop Electronics Recovery (`mock-recycler-a`).
- **Evidence:**
  ```json
  {
    "request_id": "EW-D0843536",
    "status": "draft",
    "selected_centre": "GreenLoop Electronics Recovery",
    "fee": 50.0,
    "source_url": "http://127.0.0.1:8000/mock-recycler/greenloop",
    "retrieved_at": "2026-10-22T14:44:14+00:00"
  }
  ```
- **Explanation:** GreenLoop explicitly lists Laptop in its parsed semantic HTML profile. Status: **PASSED**.

### TC2: Centre Takes Paper But Not Batteries
- **Input:** Multi-item pickup: 1 $\times$ paper and 1 $\times$ battery in Mysuru (570001), max fee ₹200.
- **Expected:** Paper accepted if supported by source; batteries rejected by paper-only centre.
- **Actual:** Paper accepted by PaperBack Materials Centre; PaperBack explicitly rejects batteries.
- **Evidence:**
  ```json
  {
    "paper_decision": "accepted",
    "paper_centre": "PaperBack Materials Centre",
    "paperback_rejected_materials": [
      "battery", "laptop", "mobile_phone", "desktop_computer",
      "television", "tablet", "keyboard_mouse", "printer"
    ],
    "paperback_source_url": "http://127.0.0.1:8000/mock-recycler/paperback"
  }
  ```
- **Explanation:** PaperBack accepts paper pulp but explicitly excludes batteries under its `#rejected-materials` HTML block. Status: **PASSED**.

### TC3: Pickup Fee Above User's Limit
- **Input:** 1 $\times$ laptop in Mysuru (570001) with a strict budget of ₹10.0.
- **Expected:** Exception approval requested; booking blocked until explicit approval.
- **Actual:** Status set to `awaiting_approval`; `POST /api/requests/{id}/book` rejected with HTTP 409 Conflict.
- **Evidence:**
  ```json
  {
    "request_id": "EW-F5381D63",
    "status": "awaiting_approval",
    "proposed_fee": 50.0,
    "user_limit": 10.0,
    "booking_blocked_before_approval": true
  }
  ```
- **Explanation:** The lowest matching fee is ₹50 (GreenLoop), which exceeds the ₹10 limit. Booking is securely blocked until the user accepts the fee exception. Status: **PASSED**.

### TC4: Pickup Requested and Verified (Never Collected)
- **Input:** 1 $\times$ laptop in Mysuru (570001), max fee ₹100. Executes `create` $\rightarrow$ `approve` $\rightarrow$ `book` $\rightarrow$ `verify`.
- **Expected:** Booking reported as `requested` and verification confirmed; never reported as physically collected.
- **Actual:** `booking_status: requested`, `verified: true`, `collection_confirmed: false`.
- **Evidence:**
  ```json
  {
    "request_id": "EW-3801F3BC",
    "booking_reference": "MOCK-59CFA5DD",
    "booking_status": "requested",
    "verified_status": "verified",
    "collection_confirmed": false,
    "display_wording": "Requested, not yet collected."
  }
  ```
- **Explanation:** Full workflow executed. Verification confirms the simulated booking without falsely claiming collection. Status: **PASSED**.

---

## 10. Deployment Guide (Vercel & Render)

### Option A: Vercel Deployment (Serverless ASGI)
1. **Repository Structure:**
   - Entry point: `api/index.py` (exposes FastAPI ASGI `app`).
   - Configuration: `vercel.json` routes all traffic to `api/index.py`.
2. **Setup on Vercel Dashboard:**
   - Import GitHub repository `https://github.com/codebyraunak/reloop`.
   - Set Framework Preset: **Other**.
   - Set Root Directory: `./`.
   - Add Environment Variables:
     - `DATABASE_URL`: Your Supabase PostgreSQL connection string.
     - `GEMINI_API_KEY`: Your Google AI Studio API key.
     - `PUBLIC_BASE_URL`: Your Vercel production domain (e.g. `https://reloop-demo.vercel.app`).
3. **Deploy:** Click **Deploy**. Vercel installs `requirements.txt` and starts the serverless function.

### Option B: Render Deployment (Web Service)
1. **Configuration:** Uses root `render.yaml` defining a Python Web Service.
2. **Settings:**
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn ecoroute.backend.main:app --host 0.0.0.0 --port $PORT`
   - Python Version: `3.11.9`
3. **Environment Variables:**
   - Add `DATABASE_URL`, `GEMINI_API_KEY`, and `PUBLIC_BASE_URL` in the Render dashboard.

---

## 11. Known Limitations

1. **Simulated Booking References:** The generated booking codes (e.g., `MOCK-59CFA5DD`) are synthetic tokens confirming coordinator handoff, not live logistics dispatch.
2. **Absence of Physical Collection:** The system tracks the pickup coordination lifecycle up to verification. In compliance with contest rules, physical dispatch or pickup collection is not performed.
3. **Mock Recycler URLs:** Recycler profile web pages are hosted within the application engine at `/mock-recycler/{slug}` for deterministic, reproducible evaluation.

---

## 12. 2–3 Minute Demo Video Script

**Target Duration:** 2 minutes 30 seconds
**Visual Setting:** Screen recording split between the ReLoop homepage ([localhost:8000](http://localhost:8000)) and the Benchmark page ([localhost:8000/tests](http://localhost:8000/tests)).

### Segment 1: Introduction & Architecture (0:00 – 0:35)
- **Voiceover:** *"Welcome to ReLoop, an intelligent e-waste and recycling pickup coordinator. Unlike standard booking portals, ReLoop uses an agentic tool loop paired with deterministic backend boundary validation, live mock recycler web scraping, and strict human approval safeguards."*
- **Action:** Open homepage. Show the ReLoop green/off-white interface, material selector, and live backend connection badge. Click "Plan with AI" to show prefilled prompt without accidental submission.

### Segment 2: Natural Language Extraction & Evidence Scraping (0:35 – 1:05)
- **Voiceover:** *"Notice our boundary validator. When a user writes 'I have 1 laptop in Mysuru and my budget is ₹100', our extractor correctly separates the ₹100 budget from the item quantity. The agent executes a 6-step bounded loop, scraping the mock recycler's live HTML profile to confirm accepted materials and fees."*
- **Action:** Submit the request. Point to the **Live Agent Trace Panel** showing each of the 6 steps executing with inputs, tool names, and timestamps. Click the GreenLoop source link showing the live parsed HTML profile.

### Segment 3: Human Approval Modal & Fee Exception Gating (1:05 – 1:45)
- **Voiceover:** *"Here we demonstrate Test Case 3: when the lowest matching fee exceeds the user's limit—say a ₹10 budget for a ₹50 fee—ReLoop blocks the booking with an HTTP 409 conflict and transitions the state to 'awaiting_approval'. The user is presented with a clear Human Approval Modal showing the exact snapshot of materials, destination, and fee discrepancy. Booking can only proceed once the human explicitly approves this immutable snapshot."*
- **Action:** Trigger the fee exception. Show the Approval Modal. Click "Approve & Confirm". Show the state updating to `approved`, then `requested`.

### Segment 4: Independent Verification & Honest Status (1:45 – 2:15)
- **Voiceover:** *"Once requested, ReLoop performs independent verification by re-crawling the recycler profile to confirm the booking parameters match live evidence. Crucially, ReLoop maintains complete honesty: our status explicitly displays 'Requested, not yet collected.' No collection status exists anywhere in the codebase."*
- **Action:** Click "Verify Booking". Show the green verified card with the visible text *"Requested, not yet collected."*

### Segment 5: Automated Benchmark Suite & Wrap-up (2:15 – 2:30)
- **Voiceover:** *"Finally, navigating to `/tests` reveals our dedicated benchmark test runner. Here we can execute TC1 through TC4 with genuine backend persistence, verifying that all official test scenarios pass with live evidence. Thank you!"*
- **Action:** Navigate to `/tests`. Click "Run All Scenarios". Show all 4 test cards turning green with full evidence payloads.

---

## 13. External Submission Checklist

This checklist outlines the mandatory items requiring external human completion for contest submission:

- [ ] **Genuine Team Photo:** Capture and attach a clear photograph showing all three registered team members together.
- [ ] **LinkedIn Publication:** Publish a post showcasing the ReLoop solution, architecture highlights, and tagging `@Ziro Digital`.
- [ ] **Real Live Screenshots:** Capture high-resolution screenshots of the deployed application on Vercel/Render, including the Trace Panel, Approval Modal, and `/tests` Benchmark runner.
- [ ] **Video Walkthrough Upload:** Record the 2–3 minute demonstration following the script in Section 12 and upload to YouTube/Loom/Drive with public access.
- [ ] **Final Presentation Deck:** Prepare the submission slide deck summarizing problem statement, architecture, safety mechanisms, and team roles.
