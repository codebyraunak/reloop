# ReLoop FastAPI backend

The backend also serves the integrated standalone ReLoop frontend at `http://localhost:8000/`. All recycler profiles and booking actions are synthetic demo data.

## Run

```bash
cd backend
python -m venv .venv
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Frontend: http://localhost:8000  
API docs: http://localhost:8000/docs  
Mock recycler pages: `/mock-recycler/greenloop`, `/mock-recycler/circular-earth`, `/mock-recycler/paperback`.

## Environment configuration

Create a `.env` file in the `backend/` folder (or copy `.env.example`):

```ini
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-flash-lite-latest
```

The model can be configured to any supported Gemini model via `GEMINI_MODEL` (e.g. `gemini-flash-lite-latest`, `gemini-3.8-flash`, etc.).

## Gemini agent & tool calling

The assistant uses the official `google-genai` SDK with function declarations / tool calling to invoke deterministic backend tools:
1. `collect_and_validate_pickup_details`: validates required fields (materials, quantity, city, max fee).
2. `search_mock_recyclers`: verifies accepted/rejected materials and operational coverage across mock recyclers.
3. `read_mock_recycler_website`: extracts profiles directly from the mock recycler webpages (`/mock-recycler/{slug}`).
4. `compare_fees_with_budget`: checks listed pickup fees against the user's budget and indicates whether fee-exception approval is required.
5. `prepare_pickup_plan_with_evidence`: prepares a pickup draft backed by source evidence.

## Safety & guardrails

- **Strict booking control**: Gemini never books pickups directly; explicit human approval is strictly required.
- **No unevidenced collection**: A booking reference is never evidence of physical collection; collection confirmation requires separate evidence.
- **Graceful fallback**: If `GEMINI_API_KEY` is missing, invalid, or rate-limited, the endpoint returns a clearly labeled fallback response (`fallback_used: true`, `[Fallback: ...]`) and never silently claims live AI succeeded.

## Tests

```bash
python -m pytest -q
```

All scenarios (TC1–TC4), Gemini tool calling, fallback mode, and the complete request-to-booking workflow are verified in the pytest suite.
