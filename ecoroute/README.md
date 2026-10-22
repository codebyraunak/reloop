# ReLoop / EcoRoute — E-waste & Recycling Pickup Coordinator

This project integrates the supplied standalone `ewaste.html` ReLoop frontend with a FastAPI backend, SQLite persistence, mock recycler source pages, human approval, mock booking, independent verification, and the four official scenario tests. The frontend design and ReLoop branding are preserved.

> **Demo safety:** recycler profiles, fees, source pages, and booking references are synthetic. No real recycler is contacted and no real pickup is arranged.

## Stack
- **Frontend:** the supplied standalone HTML, CSS, and JavaScript, served by FastAPI at `/`
- **Backend:** FastAPI + Pydantic
- **Database:** SQLite
- **Tests:** pytest + FastAPI TestClient
- **Mock source pages:** `/mock-recycler/greenloop`, `/mock-recycler/circular-earth`, `/mock-recycler/paperback`

## Run locally (Windows PowerShell)

Open a terminal in the extracted `ecoroute` folder:

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Open **http://localhost:8000**. The supplied ReLoop HTML is served directly by FastAPI, so you do not need React, Vite, or a separate frontend server.

API docs: http://localhost:8000/docs  
Health check: http://localhost:8000/api/health

## Environment configuration

Configure your Gemini API settings in `.env` (or copy `.env.example`):

```ini
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-flash-lite-latest
```

## Run locally (macOS/Linux)

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Then open http://localhost:8000.

## Run tests

In another terminal:

```bash
cd backend
python -m pytest -q
```

The frontend's **Scenario test bench** button also runs TC1–TC4 against the backend and stores each result and evidence in SQLite.

## Official test cases

| ID | Expected result | How it is demonstrated |
|---|---|---|
| TC1 | Pickup draft created | A compatible centre explicitly accepts a laptop. |
| TC2 | Batteries rejected | The paper-only centre lists batteries as rejected. |
| TC3 | Approval asked | A fee over the entered limit requires an explicit fee-exception approval. |
| TC4 | Not reported as collected | The booking remains `BOOKED`; collection needs separate evidence. |

## API routes

- `GET /api/health`
- `GET /api/materials`
- `GET /api/recyclers`
- `GET /mock-recycler/{slug}`
- `POST /api/assistant`
- `POST /api/requests`
- `GET /api/requests`
- `GET /api/requests/{request_id}`
- `POST /api/requests/{request_id}/approval`
- `POST /api/requests/{request_id}/book`
- `POST /api/requests/{request_id}/verify`
- `POST /api/tests/run`
- `POST /api/tests/run/{case_id}`
- `GET /api/tests`

## Safety and data handling

- Only item type, quantity, location and maximum fee are sent to the pickup API by the supplied frontend. Personal data is never exposed.
- Human approval is strictly required before booking. A fee above the user's limit requires explicitly accepting the exception in the Human Approval Modal.
- Booking submission is idempotent per request, preventing duplicate bookings on retries.
- Verification independently re-checks live mock recycler evidence. A booking reference is not evidence of physical collection.
- **Strictly No Physical Collection:** The status `collected` does not exist anywhere in the application. Status wording is always *"Requested, not yet collected."*
- The assistant operates under a bounded 6-step agent loop, validated deterministically at the backend boundary. It strictly cannot book pickups without explicit human approval.
