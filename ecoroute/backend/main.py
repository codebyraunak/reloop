from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import sys
from pathlib import Path
from typing import Any, Optional

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR.parent / ".env")

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

# Internal modules
from db import (
    db_connection,
    fetchone_dict,
    fetchall_dict,
    init_db,
    now_iso,
    TEST_MODE,
)
from mock_recyclers import (
    MATERIAL_LABELS,
    SEED_RECYCLERS,
    get_public_base_url,
    get_source_url,
    render_mock_recycler_html,
    DEMO_SCENARIO_OVERRIDES,
    reset_demo_scenarios,
    retrieve_mock_recycler_evidence,
    retrieve_all_mock_recyclers,
)
from agent import (
    BoundedAgentCoordinator,
    extract_assistant_intent,
    validate_pin_code,
)

logger = logging.getLogger("ecoroute.main")

FRONTEND_FILE = BASE_DIR / "static" / "index.html"
TESTS_FILE = BASE_DIR / "static" / "tests.html"
RELOOP_LOGO_FILE = BASE_DIR / "static" / "images" / "reloop-logo.png"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest").strip()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize database tables and migrations on startup
    init_db()
    yield


app = FastAPI(
    title="ReLoop — EcoRoute Pickup Coordinator",
    version="1.0.0",
    description="Evidence-based e-waste & recycling pickup coordination with human approval safeguards.",
    lifespan=lifespan,
)

# Mount static files for assets and images
static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    images_dir = static_dir / "images"
    if images_dir.exists():
        app.mount("/images", StaticFiles(directory=str(images_dir)), name="images")
        app.mount("/assets", StaticFiles(directory=str(images_dir)), name="assets")

# Configure CORS
cors_origins_raw = os.getenv("CORS_ORIGINS", "*")
if cors_origins_raw == "*":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in cors_origins_raw.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ---------------------------------------------------------------------------
# Pydantic Schemas & Boundary Validation
# ---------------------------------------------------------------------------

class ItemInput(BaseModel):
    item_type: str = Field(..., description="Supported material ID (e.g. laptop, mobile_phone, battery, paper)")
    quantity: int = Field(..., strict=True, ge=1, le=500, description="Quantity of items (must be at least 1)")


class RequestCreate(BaseModel):
    item_type: Optional[str] = Field(default=None, description="Single material ID if submitting one item type")
    quantity: Optional[int] = Field(default=None, strict=True, ge=1, le=500, description="Quantity for single item request")
    items: Optional[list[ItemInput]] = Field(default=None, description="Multiple items in one pickup request")
    city: str = Field(..., min_length=1, max_length=100)
    pincode: str = Field(..., pattern=r"^[1-9][0-9]{5}$")
    max_fee: float = Field(..., ge=0.0, le=100000.0, description="User fee budget limit (cannot be negative)")
    preferred_centre: Optional[str] = None

    @field_validator("city", "pincode", mode="before")
    @classmethod
    def strip_location_fields(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class ApprovalInput(BaseModel):
    approved: bool
    accept_fee_exception: bool = False
    acknowledge_partial: bool = False


class AssistantInput(BaseModel):
    message: str = Field(..., min_length=1, max_length=1000)


class DemoScenarioInput(BaseModel):
    slug: str = Field(..., min_length=1)
    scenario: Optional[str] = None


def demo_controls_enabled() -> bool:
    return os.getenv("DEMO_CONTROLS", "1").strip().lower() not in {"0", "false", "off", "no"}


def normalize_demo_scenario(scenario: Optional[str]) -> Optional[str]:
    if scenario is None:
        return None
    if scenario in {"fee_up", "pickup_unavailable"}:
        return scenario
    if scenario.startswith("stops_accepting_"):
        material = scenario.removeprefix("stops_accepting_")
        if material in MATERIAL_LABELS:
            return scenario
    raise HTTPException(status_code=422, detail="Unsupported demo scenario.")


# ---------------------------------------------------------------------------
# Backward Compatibility Shims
# ---------------------------------------------------------------------------
MOCK_RECYCLERS = [
    {
        "id": r["id"],
        "slug": r["slug"],
        "name": r["name"],
        "area": r["area"],
        "accepted_materials": r["accepted_materials"],
        "rejected_materials": r["rejected_materials"],
        "pickup_fee": r["pickup_fee_inr"],
        "currency": "INR",
        "pickup_available": r["pickup_available"],
        "source_url": get_source_url(r["slug"]),
        "notes": r["notes"],
    }
    for r in SEED_RECYCLERS
]


def public_recycler(recycler: dict[str, Any]) -> dict[str, Any]:
    return {
        **recycler,
        "is_mock_data": True,
        "accepted_material_labels": [MATERIAL_LABELS.get(x, x) for x in recycler.get("accepted_materials", [])],
        "rejected_material_labels": [MATERIAL_LABELS.get(x, x) for x in recycler.get("rejected_materials", [])],
    }


def sanitize_collected_status(value: Any) -> Any:
    """Normalizes legacy collected statuses anywhere in API response data."""
    if isinstance(value, dict):
        return {
            key: (
                "requested"
                if key == "status" and isinstance(item, str) and item.casefold() == "collected"
                else sanitize_collected_status(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_collected_status(item) for item in value]
    return value


def make_plan(request: RequestCreate) -> dict[str, Any]:
    """Evaluates pickup request through the deterministic evidence-based agent."""
    # Normalize items
    items_list = []
    if request.items:
        items_list = [{"item_type": i.item_type, "quantity": i.quantity} for i in request.items]
    elif request.item_type:
        qty = request.quantity if request.quantity is not None else 1
        items_list = [{"item_type": request.item_type, "quantity": qty}]
    else:
        raise HTTPException(status_code=422, detail="At least one item or item_type must be specified.")

    # Parse city and PIN
    city = request.city or ""
    pincode = request.pincode or ""
    if not city or not pincode:
        loc = (request.location or "").strip()
        pin_match = re.search(r"\b([1-9][0-9]{5})\b", loc)
        if pin_match:
            pincode = pin_match.group(1)
            city = loc.replace(pincode, "").strip() or "Mysuru"
        else:
            city = loc or "Mysuru"
            pincode = "570001"

    coordinator = BoundedAgentCoordinator(app_instance=app)
    result = coordinator.run_agent_workflow(
        items=items_list,
        city=city,
        pincode=pincode,
        max_fee=request.max_fee,
        preferred_centre=request.preferred_centre,
    )
    if result.get("status") == "failed" and result.get("plan") is None:
        raise HTTPException(status_code=422, detail=result.get("reason", "Validation error"))

    plan = result["plan"]
    plan["agent_trace"] = result["agent_trace"]
    return plan


def request_row(row_dict: dict[str, Any]) -> dict[str, Any]:
    """Enriches a request record with parsed JSON, approval, booking, and traces."""
    res = dict(row_dict)
    if res.get("status", "").casefold() == "collected":
        res["status"] = "requested"
    req_id = res["id"]
    if "plan_json" in res and res["plan_json"]:
        res["plan"] = json.loads(res.pop("plan_json"))
    else:
        res["plan"] = {}

    if "items_json" in res and res["items_json"]:
        res["items"] = json.loads(res.pop("items_json"))

    with db_connection() as conn:
        appr_cursor = conn.execute("SELECT * FROM approvals WHERE request_id = ?", (req_id,))
        appr_row = fetchone_dict(appr_cursor)
        book_cursor = conn.execute("SELECT * FROM bookings WHERE request_id = ?", (req_id,))
        book_row = fetchone_dict(book_cursor)
        trace_cursor = conn.execute("SELECT * FROM agent_traces WHERE request_id = ? ORDER BY step ASC", (req_id,))
        traces = fetchall_dict(trace_cursor)

    if appr_row:
        appr = dict(appr_row)
        if appr.get("approved_plan_json"):
            appr["approved_plan"] = json.loads(appr.pop("approved_plan_json"))
        res["approval"] = appr
    else:
        res["approval"] = None

    if book_row:
        book = dict(book_row)
        if book.get("status", "").casefold() == "collected":
            book["status"] = "requested"
        if book.get("approved_snapshot_json"):
            book["approved_snapshot"] = json.loads(book.pop("approved_snapshot_json"))
        for column, public_name in (
            ("verification_checks", "checks"),
            ("drift_json", "drift"),
        ):
            if book.get(column):
                book[public_name] = json.loads(book.pop(column))
        book["display_status"] = "Requested, not yet collected."
        res["booking"] = book
    else:
        res["booking"] = None

    res["agent_traces"] = [
        {
            "step": t["step"],
            "tool": t["tool"],
            "input": json.loads(t["input_json"]) if t.get("input_json") else {},
            "output": json.loads(t["output_json"]) if t.get("output_json") else {},
            "evidence": json.loads(t["evidence_json"]) if t.get("evidence_json") else None,
            "error": json.loads(t["error_json"]) if t.get("error_json") else None,
            "timestamp": t["timestamp"],
        }
        for t in traces
    ]
    return sanitize_collected_status(res)


# ---------------------------------------------------------------------------
# Frontend & Health Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def frontend():
    if not FRONTEND_FILE.exists():
        raise HTTPException(status_code=500, detail="ReLoop frontend template missing.")
    return FRONTEND_FILE.read_text(encoding="utf-8")


@app.get("/tests", response_class=HTMLResponse)
def tests_page():
    if not TESTS_FILE.exists():
        raise HTTPException(status_code=500, detail="ReLoop tests page template missing.")
    return TESTS_FILE.read_text(encoding="utf-8")


@app.get("/favicon/{filename}")
def favicon(filename: str):
    if filename != "reloop-logo.png":
        raise HTTPException(status_code=404, detail="Favicon not found.")
    if not RELOOP_LOGO_FILE.is_file():
        raise HTTPException(status_code=404, detail="Favicon not found.")
    return FileResponse(RELOOP_LOGO_FILE, media_type="image/png")


@app.get("/api/health")
def health() -> dict[str, Any]:
    db_status = "connected"
    try:
        with db_connection() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        db_status = f"error: {type(exc).__name__}"

    return {
        "status": "ok",
        "service": "EcoRoute",
        "version": "1.0.0",
        "mode": "evidence-backed-coordinator",
        "database_status": db_status,
        "database_dialect": (
            "postgres"
            if os.getenv("DATABASE_URL", "").strip() and not TEST_MODE
            else "sqlite"
        ),
        "demo_controls_enabled": demo_controls_enabled(),
        "gemini_configured": bool(GEMINI_API_KEY),
        "gemini_model": GEMINI_MODEL if GEMINI_API_KEY else None,
        "timestamp": now_iso(),
    }


@app.get("/api/materials")
def materials() -> list[dict[str, str]]:
    return [{"id": key, "label": label} for key, label in MATERIAL_LABELS.items()]


@app.get("/api/recyclers")
def recyclers() -> list[dict[str, Any]]:
    return [public_recycler(r) for r in MOCK_RECYCLERS]


@app.get("/mock-recycler/{slug}", response_class=HTMLResponse)
def mock_recycler_page(slug: str, scenario: Optional[str] = None):
    if scenario and not demo_controls_enabled():
        raise HTTPException(status_code=404, detail="Demo scenarios are disabled.")
    selected_scenario = normalize_demo_scenario(
        scenario or (
            DEMO_SCENARIO_OVERRIDES.get(slug.strip().lower())
            if demo_controls_enabled()
            else None
        )
    )
    try:
        html = render_mock_recycler_html(slug, scenario=selected_scenario)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not html:
        raise HTTPException(status_code=404, detail=f"Mock recycler profile '{slug}' not found.")
    return HTMLResponse(content=html, status_code=200)


@app.post("/api/demo/scenario")
def set_demo_scenario(payload: DemoScenarioInput) -> dict[str, Any]:
    if not demo_controls_enabled():
        raise HTTPException(status_code=404, detail="Demo controls are disabled.")
    slug = payload.slug.strip().lower()
    if not any(seed["slug"] == slug for seed in SEED_RECYCLERS):
        raise HTTPException(status_code=404, detail="Recycler profile not found.")
    scenario = normalize_demo_scenario(payload.scenario)
    if scenario is None:
        DEMO_SCENARIO_OVERRIDES.pop(slug, None)
    else:
        DEMO_SCENARIO_OVERRIDES[slug] = scenario
    return {"slug": slug, "scenario": scenario, "enabled": True}


# ---------------------------------------------------------------------------
# Gemini Tools Compatibility Functions
# ---------------------------------------------------------------------------

def collect_and_validate_pickup_details(item_type: str, quantity: int, location: str, max_fee: float) -> str:
    raw_item = (item_type or "").strip().lower()
    resolved = MATERIAL_ALIASES.get(raw_item, raw_item)
    valid_material = resolved in MATERIAL_LABELS
    missing = []
    if not valid_material:
        missing.append("valid material type (supported: " + ", ".join(MATERIAL_LABELS.keys()) + ")")
    if not location or len(location.strip()) < 2:
        missing.append("service location (e.g. Mysuru)")
    if max_fee <= 0:
        missing.append("maximum fee budget in INR")

    return json.dumps({
        "status": "COMPLETE" if not missing else "INCOMPLETE",
        "item_type": resolved if valid_material else None,
        "item_label": MATERIAL_LABELS.get(resolved) if valid_material else None,
        "quantity": max(1, min(500, int(quantity or 1))),
        "location": location.strip() if location else "",
        "max_fee": float(max_fee),
        "missing_details": missing,
    })


def search_mock_recyclers(item_type: str, location: str) -> str:
    raw = (item_type or "").strip().lower()
    resolved = MATERIAL_ALIASES.get(raw, raw)
    if resolved not in MATERIAL_LABELS:
        return json.dumps({"error": f"Item type '{item_type}' is not recognized."})

    recyclers = retrieve_all_mock_recyclers(app_instance=app)
    matches = []
    rejected = []
    for r in recyclers:
        in_area = location.strip().lower() in r["area"].lower() or r["area"].lower() in location.strip().lower()
        if resolved in r["accepted_materials"]:
            matches.append({
                "recycler_id": r["id"],
                "name": r["name"],
                "area": r["area"],
                "in_area": in_area,
                "pickup_fee": r["pickup_fee"],
                "currency": "INR",
                "pickup_available": r["pickup_available"],
                "source_url": r["source_url"],
            })
        elif resolved in r["rejected_materials"]:
            rejected.append({
                "recycler_id": r["id"],
                "name": r["name"],
                "reason": f"{MATERIAL_LABELS[resolved]} is explicitly not accepted.",
                "source_url": r["source_url"],
            })
    return json.dumps({"item_type": resolved, "location": location, "matches": matches, "rejected_centres": rejected})


def read_mock_recycler_website(slug: str) -> str:
    evidence = retrieve_mock_recycler_evidence(slug, app_instance=app)
    if evidence.get("status") == "unresolved":
        return json.dumps({"error": evidence.get("error", "Not found")})
    return json.dumps({
        "name": evidence["name"],
        "area": evidence["area"],
        "accepted_materials": [MATERIAL_LABELS.get(m, m) for m in evidence["accepted_materials"]],
        "rejected_materials": [MATERIAL_LABELS.get(m, m) for m in evidence["rejected_materials"]],
        "pickup_fee": evidence["pickup_fee"],
        "currency": "INR",
        "pickup_available": evidence["pickup_available"],
        "source_url": evidence["source_url"],
        "retrieved_at": evidence["retrieved_at"],
        "is_mock_data": evidence["is_mock_data"],
        "notes": evidence.get("notes", ""),
    })


def compare_fees_with_budget(item_type: str, location: str, max_fee: float) -> str:
    search_data = json.loads(search_mock_recyclers(item_type, location))
    if "error" in search_data:
        return json.dumps(search_data)
    viable = [m for m in search_data["matches"] if m["in_area"] and m["pickup_available"]]
    viable.sort(key=lambda m: m["pickup_fee"])
    if not viable:
        return json.dumps({"viable_recyclers": 0, "status": "NO_MATCH", "message": f"No viable recyclers found for {item_type} in {location}."})
    cheapest = viable[0]
    fee_within_budget = cheapest["pickup_fee"] <= max_fee
    return json.dumps({
        "selected_recycler": cheapest["name"],
        "pickup_fee": cheapest["pickup_fee"],
        "user_budget": max_fee,
        "fee_within_budget": fee_within_budget,
        "approval_required": not fee_within_budget,
        "message": (
            f"Pickup fee is ₹{cheapest['pickup_fee']}, which fits within your ₹{max_fee} budget."
            if fee_within_budget
            else f"Pickup fee is ₹{cheapest['pickup_fee']}, which exceeds your ₹{max_fee} budget. Explicit human approval of the fee exception will be required."
        ),
    })


def prepare_pickup_plan_with_evidence(item_type: str, quantity: int, location: str, max_fee: float) -> str:
    try:
        clean_location = (location or "").strip()
        pin_match = re.search(r"\b([1-9][0-9]{5})\b", clean_location)
        pincode = pin_match.group(1) if pin_match else "570001"
        city = clean_location.replace(pincode, "").strip() if pin_match else clean_location
        req = RequestCreate(
            item_type=item_type,
            quantity=quantity,
            city=city or "Mysuru",
            pincode=pincode,
            max_fee=max_fee,
        )
        plan = make_plan(req)
        selected = plan.get("selected_recycler")
        return json.dumps({
            "status": plan["status"].upper(),
            "reason": plan["reason"],
            "selected_recycler": selected["recycler_name"] if selected else None,
            "fee": selected["fee"] if selected else None,
            "evidence": f"Mock source lists {MATERIAL_LABELS.get(item_type, item_type)} as accepted; pickup fee listed as ₹{selected['fee']}." if selected else None,
            "source_url": selected["source_url"] if selected else None,
            "requires_human_approval": True,
        })
    except Exception as exc:
        return json.dumps({"status": "ERROR", "error": str(exc)})


# ---------------------------------------------------------------------------
# Assistant Endpoint
# ---------------------------------------------------------------------------

@app.post("/api/assistant")
def assistant(payload: AssistantInput) -> dict[str, Any]:
    """Gemini-powered recycling coordinator with strict booking safety and accurate entity extraction."""
    extracted = extract_assistant_intent(payload.message)
    primary_item = extracted["primary_item"]
    quantity = extracted["quantity"]
    city = extracted["city"] or "Mysuru"
    pincode = extracted["pincode"] or "570001"
    max_fee = extracted["max_fee"] or 100.0

    # Deterministic fallback response text
    lower_msg = payload.message.lower()
    if any(word in lower_msg for word in ("approval", "approve", "booking", "book", "confirm")):
        fallback_reply = (
            "Submitting the pickup form creates a pickup draft with evidenced centre matches. "
            "You will see the exact materials, destination and fee for explicit human approval. "
            "A booking request never establishes physical collection."
        )
    elif not primary_item:
        fallback_reply = (
            "Tell me which items you would like to recycle (e.g. 1 laptop, mobile phone, battery, or paper) "
            "and your city or PIN code. I can verify recycler acceptance, but human approval is strictly required before any booking."
        )
    else:
        label = MATERIAL_LABELS.get(primary_item, primary_item)
        fallback_reply = (
            f"I understood {quantity} {label.lower()}. Submit the pickup planner form with your city and fee budget "
            f"to generate an evidence-backed pickup plan. No booking has been made yet."
        )

    # If Gemini is not configured or in test mode without external calls, return labeled deterministic fallback
    if not GEMINI_API_KEY or TEST_MODE:
        return {
            "reply": f"[Fallback: Rule-based agent active] {fallback_reply}" if not GEMINI_API_KEY else fallback_reply,
            "item_type": primary_item,
            "quantity": quantity,
            "city": city,
            "pincode": pincode,
            "max_fee": max_fee,
            "items": extracted["items"],
            "needs_location": not bool(extracted["city"]),
            "booking_performed": False,
            "ai_powered": False if not GEMINI_API_KEY else True,
            "fallback_used": not bool(GEMINI_API_KEY),
            "tool_called": "collect_and_validate_pickup_details",
            "tool_result": {
                "item_type": primary_item,
                "quantity": quantity,
                "location": f"{city} {pincode}".strip(),
                "max_fee": max_fee,
            },
            "error": "GEMINI_API_KEY not configured." if not GEMINI_API_KEY else None,
        }

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=GEMINI_API_KEY)
        system_instruction = (
            "You are ReLoop / EcoRoute's AI recycling coordinator. "
            "Help the user coordinate electronic waste and recyclables (laptops, phones, batteries, paper). "
            "STRICT BOOKING & SAFETY RULES: "
            "1. You CANNOT create bookings or confirm physical collections. Human approval is strictly required before any booking. "
            "2. A booking or pickup draft is never evidence that items have been collected. "
            "3. Keep user responses concise, clear, and friendly (2-3 sentences max). "
            "4. Remind the user that no booking has been made yet."
        )

        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=payload.message.strip(),
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.2,
            ),
        )
        reply = resp.text.strip() if resp.text else fallback_reply

        return {
            "reply": reply,
            "item_type": primary_item,
            "quantity": quantity,
            "city": city,
            "pincode": pincode,
            "max_fee": max_fee,
            "items": extracted["items"],
            "needs_location": not bool(extracted["city"]),
            "booking_performed": False,
            "ai_powered": True,
            "fallback_used": False,
            "tool_called": "collect_and_validate_pickup_details",
            "tool_result": {
                "item_type": primary_item,
                "quantity": quantity,
                "location": f"{city} {pincode}".strip(),
                "max_fee": max_fee,
            },
            "error": None,
        }
    except Exception as exc:
        logger.warning("Gemini assistant call encountered error: %s", exc)
        return {
            "reply": f"[Fallback: {type(exc).__name__}] {fallback_reply}",
            "item_type": primary_item,
            "quantity": quantity,
            "city": city,
            "pincode": pincode,
            "max_fee": max_fee,
            "items": extracted["items"],
            "needs_location": True,
            "booking_performed": False,
            "ai_powered": False,
            "fallback_used": True,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Pickup Requests Boundary & Lifecycle
# ---------------------------------------------------------------------------

@app.post("/api/requests")
def create_request(payload: RequestCreate, request: Request) -> dict[str, Any]:
    # Boundary validation
    # 1. Supported item type and quantity >= 1
    items_to_save = []
    if payload.items:
        for idx, it in enumerate(payload.items):
            if it.item_type not in MATERIAL_LABELS:
                raise HTTPException(status_code=422, detail=f"Unsupported item type '{it.item_type}'. Supported: {', '.join(MATERIAL_LABELS.keys())}.")
            if it.quantity < 1:
                raise HTTPException(status_code=422, detail=f"Item #{idx+1} quantity must be >= 1.")
            items_to_save.append({"item_type": it.item_type, "quantity": it.quantity})
    elif payload.item_type:
        if payload.item_type not in MATERIAL_LABELS:
            raise HTTPException(status_code=422, detail=f"Unsupported item type '{payload.item_type}'. Supported: {', '.join(MATERIAL_LABELS.keys())}.")
        if payload.quantity is None:
            raise HTTPException(status_code=422, detail="Quantity is required for a single item request.")
        qty = payload.quantity
        items_to_save.append({"item_type": payload.item_type, "quantity": qty})
    else:
        raise HTTPException(status_code=422, detail="At least one item must be provided.")

    # Location is validated as two independent fields by RequestCreate.
    city = payload.city
    pincode = payload.pincode
    if not validate_pin_code(pincode):
        raise HTTPException(status_code=422, detail="A valid 6-digit Indian PIN code is required (e.g. 570001).")

    # 3. Non-negative fee limit
    if payload.max_fee < 0:
        raise HTTPException(status_code=422, detail="Maximum fee limit cannot be negative.")

    # Run bounded agent workflow
    request_id = "EW-" + uuid.uuid4().hex[:8].upper()
    coordinator = BoundedAgentCoordinator(
        request_id=request_id,
        app_instance=app,
        request_base_url=str(request.base_url).rstrip("/"),
    )
    agent_res = coordinator.run_agent_workflow(
        items=items_to_save,
        city=city,
        pincode=pincode,
        max_fee=payload.max_fee,
        preferred_centre=payload.preferred_centre,
    )
    if agent_res.get("status") == "failed" and agent_res.get("plan") is None:
        raise HTTPException(status_code=422, detail=agent_res.get("reason", "Validation failure"))

    plan = agent_res["plan"]
    status = plan["status"]
    timestamp = now_iso()
    primary_material = items_to_save[0]["item_type"]
    primary_qty = items_to_save[0]["quantity"]

    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO requests(id, item_type, quantity, location, city, pincode, max_fee, items_json, status, plan_json, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                primary_material,
                primary_qty,
                f"{city} {pincode}".strip(),
                city,
                pincode,
                payload.max_fee,
                json.dumps(items_to_save),
                status,
                json.dumps(plan),
                timestamp,
                timestamp,
            ),
        )
        row = fetchone_dict(conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)))

    return request_row(row)


@app.get("/api/requests")
def list_requests(request_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Only serves a specific request; the collection is never publicly enumerable."""
    if not request_id:
        raise HTTPException(status_code=400, detail="A request_id is required.")
    with db_connection() as conn:
        cursor = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id.strip(),))
        row = fetchone_dict(cursor)
    if not row:
        raise HTTPException(status_code=404, detail="Pickup request not found.")
    rows = [row]
    return [request_row(r) for r in rows]


@app.get("/api/requests/{request_id}")
def get_request(request_id: str) -> dict[str, Any]:
    with db_connection() as conn:
        cursor = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
        row = fetchone_dict(cursor)
    if not row:
        raise HTTPException(status_code=404, detail="Pickup request not found.")
    return request_row(row)


@app.post("/api/requests/{request_id}/approval")
def approve_request(request_id: str, payload: ApprovalInput) -> dict[str, Any]:
    """Human approval endpoint with immutable snapshot storage and fee-exception gating."""
    with db_connection() as conn:
        cursor = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
        row = fetchone_dict(cursor)
        if not row:
            raise HTTPException(status_code=404, detail="Pickup request not found.")

        # Check if booking already exists
        existing_book = fetchone_dict(conn.execute("SELECT * FROM bookings WHERE request_id = ?", (request_id,)))
        if existing_book:
            raise HTTPException(
                status_code=409,
                detail="A booking already exists for this request; approval cannot be changed after booking."
            )

        plan = json.loads(row["plan_json"])
        selected = plan.get("selected_recycler")
        total_fee = plan.get("total_fee", 0.0)
        current_status = row["status"].casefold()
        if current_status not in {"draft", "awaiting_approval"}:
            raise HTTPException(
                status_code=409,
                detail=f"Approval is only available for draft or awaiting_approval requests, not '{current_status}'.",
            )

        # Gate on fee exception: if proposed fee exceeds user's limit, explicit accept_fee_exception is required
        if total_fee > row["max_fee"] and not payload.accept_fee_exception and payload.approved:
            raise HTTPException(
                status_code=409,
                detail="Fee exceeds your limit. Explicit fee exception approval is required before proceeding."
            )

        if not selected and payload.approved and not any(i.get("suggested_recycler") for i in plan.get("items", [])):
            raise HTTPException(
                status_code=409,
                detail="Cannot approve: no matching recycler was evidenced for the requested materials."
            )

        partial_items = [
            item for item in plan.get("items", [])
            if item.get("decision") in {"rejected", "unresolved"}
        ]
        if payload.approved and partial_items and not payload.acknowledge_partial:
            raise HTTPException(
                status_code=409,
                detail="Explicit acknowledgement of rejected or unresolved items is required.",
            )

        timestamp = now_iso()
        new_status = "approved" if payload.approved else "declined"
        plan["approval_decision"] = {
            "approved": payload.approved,
            "accepted_fee_exception": payload.accept_fee_exception,
            "acknowledged_partial": payload.acknowledge_partial,
            "approved_at": timestamp,
            "version": "1.0",
        }
        approved_snapshot = json.dumps(plan, sort_keys=True, separators=(",", ":"))
        snapshot_hash = hashlib.sha256(approved_snapshot.encode("utf-8")).hexdigest()

        # Store immutable approved snapshot in approvals table
        conn.execute(
            """
            INSERT INTO approvals(request_id, approved, accept_fee_exception, approved_plan_json, snapshot_hash, approved_version, approved_at)
            VALUES(?, ?, ?, ?, ?, '1.0', ?)
            """,
            (
                request_id,
                int(payload.approved),
                int(payload.accept_fee_exception),
                approved_snapshot,
                snapshot_hash,
                timestamp,
            ),
        )

        conn.execute(
            "UPDATE requests SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, timestamp, request_id),
        )
        updated_row = fetchone_dict(conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)))

    return request_row(updated_row)


@app.post("/api/requests/{request_id}/book")
def book_request(request_id: str) -> dict[str, Any]:
    """Books a pickup strictly from the immutable approved snapshot with idempotency."""
    with db_connection() as conn:
        req_cursor = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
        row = fetchone_dict(req_cursor)
        if not row:
            raise HTTPException(status_code=404, detail="Pickup request not found.")

        # Idempotency check: if booking already exists, return existing reference without creating duplicate
        existing_book = fetchone_dict(conn.execute("SELECT * FROM bookings WHERE request_id = ?", (request_id,)))
        if existing_book:
            return {
                "request_id": request_id,
                "reference": existing_book["reference"],
                "status": (
                    "requested"
                    if existing_book["status"].casefold() == "collected"
                    else existing_book["status"]
                ),
                "display_status": "Requested, not yet collected.",
                "message": "Existing booking returned; duplicate submission prevented.",
            }

        current_status = row["status"]
        if current_status == "draft":
            raise HTTPException(
                status_code=409,
                detail="A draft pickup plan cannot be booked without human approval."
            )
        if current_status == "awaiting_approval":
            raise HTTPException(
                status_code=409,
                detail="Human approval is required before booking."
            )
        if current_status in ("failed", "declined", "unresolved"):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot book a pickup in '{current_status}' state."
            )

        # Authorization check: verify approval record in database
        appr_cursor = conn.execute("SELECT * FROM approvals WHERE request_id = ?", (request_id,))
        approval = fetchone_dict(appr_cursor)
        if not approval or not approval["approved"]:
            raise HTTPException(status_code=409, detail="Human approval is required before booking.")

        # Book strictly from the immutable approved snapshot
        approved_snapshot = approval["approved_plan_json"]
        plan = json.loads(approved_snapshot)
        snapshot_hash = approval.get("snapshot_hash") or hashlib.sha256(
            approved_snapshot.encode("utf-8")
        ).hexdigest()
        centres = plan.get("centres") or []
        if not centres:
            grouped: dict[str, dict[str, Any]] = {}
            for item in plan.get("items", []):
                assignment = item.get("suggested_recycler")
                if not assignment:
                    continue
                centre_id = assignment.get("recycler_id")
                centre = grouped.setdefault(centre_id, {
                    "recycler_id": centre_id,
                    "recycler_name": assignment.get("recycler_name"),
                    "fee": assignment.get("fee"),
                    "source_url": assignment.get("source_url"),
                    "retrieved_at": assignment.get("retrieved_at"),
                    "items": [],
                })
                centre["items"].append({
                    "item_type": item.get("item_type"),
                    "quantity": item.get("quantity"),
                })
            centres = list(grouped.values())
            plan["centres"] = centres
            approved_snapshot = json.dumps(plan, sort_keys=True, separators=(",", ":"))
            snapshot_hash = hashlib.sha256(approved_snapshot.encode("utf-8")).hexdigest()
        if not centres:
            raise HTTPException(status_code=409, detail="No evidenced recycler exists in the approved snapshot.")

        reference_digest = hashlib.sha256(
            f"{request_id}:{snapshot_hash}".encode("utf-8")
        ).hexdigest()[:12].upper()
        reference = "MOCK-" + reference_digest
        timestamp = now_iso()

        # The booking action moves state to requested. It NEVER marks items collected!
        conn.execute(
            """
            INSERT INTO bookings(request_id, reference, status, idempotency_key, approved_snapshot_json, snapshot_hash, submitted_at, verified)
            VALUES(?, ?, 'requested', ?, ?, ?, ?, 0)
            ON CONFLICT(request_id) DO NOTHING
            """,
            (request_id, reference, request_id, approved_snapshot, snapshot_hash, timestamp),
        )
        existing_book = fetchone_dict(conn.execute(
            "SELECT * FROM bookings WHERE request_id = ?", (request_id,)
        ))
        if not existing_book:
            raise HTTPException(status_code=500, detail="Booking insert did not create a booking record.")
        reference = existing_book["reference"]
        timestamp = existing_book["submitted_at"]
        conn.execute(
            "UPDATE requests SET status = 'requested', updated_at = ? WHERE id = ?",
            (timestamp, request_id),
        )

    return {
        "request_id": request_id,
        "reference": reference,
        "status": "requested",
        "display_status": "Requested, not yet collected.",
        "message": "Mock pickup booking requested. Physical collection is not confirmed.",
        "submitted_at": timestamp,
    }


@app.post("/api/requests/{request_id}/verify")
def verify_booking(request_id: str) -> dict[str, Any]:
    """Independently verifies booking against live mock recycler evidence.
    Verification NEVER claims items were physically collected.
    """
    with db_connection() as conn:
        book_cursor = conn.execute("SELECT * FROM bookings WHERE request_id = ?", (request_id,))
        booking = fetchone_dict(book_cursor)
        if not booking:
            raise HTTPException(status_code=409, detail="No booking exists to verify.")

        appr_cursor = conn.execute("SELECT * FROM approvals WHERE request_id = ?", (request_id,))
        approval = fetchone_dict(appr_cursor)
        if not approval:
            raise HTTPException(status_code=409, detail="No approval record associated with booking.")

        approved_snapshot_json = booking["approved_snapshot_json"] or approval["approved_plan_json"]
        approved_plan = json.loads(approved_snapshot_json)
        centres = approved_plan.get("centres") or []
        if not centres:
            grouped: dict[str, dict[str, Any]] = {}
            for item in approved_plan.get("items", []):
                assignment = item.get("suggested_recycler")
                if not assignment:
                    continue
                centre_id = assignment.get("recycler_id")
                centre = grouped.setdefault(centre_id, {
                    "recycler_id": centre_id,
                    "recycler_name": assignment.get("recycler_name"),
                    "fee": assignment.get("fee"),
                    "items": [],
                })
                centre["items"].append({
                    "item_type": item.get("item_type"),
                    "quantity": item.get("quantity"),
                })
            centres = list(grouped.values())

        checks: list[dict[str, Any]] = []
        drift: list[dict[str, Any]] = []
        unresolved = False

        stored_hash = booking.get("snapshot_hash") or approval.get("snapshot_hash")
        computed_hash = hashlib.sha256(approved_snapshot_json.encode("utf-8")).hexdigest()
        checks.append({
            "check": "approved_snapshot_hash",
            "intended": stored_hash,
            "observed": computed_hash,
            "ok": bool(stored_hash) and stored_hash == computed_hash,
        })
        expected_reference = "MOCK-" + hashlib.sha256(
            f"{request_id}:{stored_hash or ''}".encode("utf-8")
        ).hexdigest()[:12].upper()
        checks.append({
            "check": "booking_reference",
            "intended": expected_reference,
            "observed": booking["reference"],
            "ok": booking["reference"] == expected_reference,
        })
        if not stored_hash or stored_hash != computed_hash:
            drift.append({
                "centre": "booking",
                "field": "snapshot_hash",
                "approved_value": stored_hash,
                "live_value": computed_hash,
            })
        if booking["reference"] != expected_reference:
            drift.append({
                "centre": "booking",
                "field": "reference",
                "approved_value": expected_reference,
                "live_value": booking["reference"],
            })

        for centre in centres:
            centre_id = centre.get("recycler_id")
            seed = next((entry for entry in SEED_RECYCLERS if entry["id"] == centre_id), None)
            display_name = centre.get("recycler_name") or centre_id or "unknown centre"
            if not seed:
                unresolved = True
                checks.append({
                    "check": f"{display_name}:profile_available",
                    "intended": "known recycler profile",
                    "observed": "profile mapping unavailable",
                    "ok": False,
                })
                continue

            live = retrieve_mock_recycler_evidence(seed["slug"], app_instance=app)
            if live.get("status") == "unresolved" or not live.get("evidence_verified"):
                unresolved = True
                checks.append({
                    "check": f"{display_name}:profile_available",
                    "intended": "retrievable and parseable profile",
                    "observed": live.get("error", "profile unresolved"),
                    "ok": False,
                })
                continue
            profile_ok = live.get("id") == centre_id
            checks.append({
                "check": f"{display_name}:profile_available",
                "intended": centre_id,
                "observed": live.get("id"),
                "ok": profile_ok,
            })
            if not profile_ok:
                drift.append({
                    "centre": display_name,
                    "field": "recycler_id",
                    "approved_value": centre_id,
                    "live_value": live.get("id"),
                })

            intended_fee = centre.get("fee")
            observed_fee = live.get("pickup_fee")
            fee_ok = intended_fee == observed_fee
            checks.append({
                "check": f"{display_name}:fee",
                "intended": intended_fee,
                "observed": observed_fee,
                "ok": fee_ok,
            })
            if not fee_ok:
                drift.append({
                    "centre": display_name,
                    "field": "fee",
                    "approved_value": intended_fee,
                    "live_value": observed_fee,
                })

            pickup_ok = live.get("pickup_available") is True
            checks.append({
                "check": f"{display_name}:pickup_available",
                "intended": True,
                "observed": live.get("pickup_available"),
                "ok": pickup_ok,
            })
            if not pickup_ok:
                drift.append({
                    "centre": display_name,
                    "field": "pickup_available",
                    "approved_value": True,
                    "live_value": live.get("pickup_available"),
                })

            for assigned in centre.get("items", []):
                material = assigned.get("item_type")
                accepted = material in live.get("accepted_materials", [])
                checks.append({
                    "check": f"{display_name}:accepts:{material}",
                    "intended": True,
                    "observed": accepted,
                    "ok": accepted,
                })
                if not accepted:
                    drift.append({
                        "centre": display_name,
                        "field": f"accepts:{material}",
                        "approved_value": True,
                        "live_value": accepted,
                    })

        all_ok = all(check["ok"] for check in checks)
        outcome = "unresolved" if unresolved else "verified" if all_ok else "failed"
        timestamp = now_iso()
        verified_at = timestamp if outcome == "verified" else None
        if outcome == "verified":
            reason = "Live profiles match the approved materials, fees, pickup availability, snapshot hash, and booking reference."
        elif outcome == "unresolved":
            reason = "Verification is unresolved because one or more recycler profiles could not be retrieved or parsed."
        else:
            reason = "Verification failed because live evidence or booking data differs from the approved snapshot."

        conn.execute(
            """
            UPDATE bookings
            SET status = ?, verified = ?, verified_at = ?, verification_evidence = ?,
                verification_checked_at = ?, verification_checks = ?,
                verification_outcome = ?, drift_json = ?
            WHERE request_id = ?
            """,
            (
                outcome,
                int(outcome == "verified"),
                verified_at,
                reason,
                timestamp,
                json.dumps(checks),
                outcome,
                json.dumps(drift),
                request_id,
            ),
        )
        conn.execute(
            "UPDATE requests SET status = ?, updated_at = ? WHERE id = ?",
            (outcome, timestamp, request_id),
        )
        final_status = outcome

    return {
        "request_id": request_id,
        "reference": booking["reference"],
        "status": final_status,
        "verified": final_status == "verified",
        "collection_confirmed": False,
        "display_status": "Requested, not yet collected.",
        "evidence": reason,
        "checks": checks,
        "drift": drift,
        "verified_at": verified_at,
        "checked_at": timestamp,
    }


@app.post("/api/requests/{request_id}/replan")
def replan_request(request_id: str, request: Request) -> dict[str, Any]:
    """Creates an unapproved replacement plan after a failed live-evidence check."""
    with db_connection() as conn:
        booking = fetchone_dict(conn.execute(
            "SELECT * FROM bookings WHERE request_id = ?", (request_id,)
        ))
        original = fetchone_dict(conn.execute(
            "SELECT * FROM requests WHERE id = ?", (request_id,)
        ))
    if not original:
        raise HTTPException(status_code=404, detail="Pickup request not found.")
    if not booking:
        raise HTTPException(status_code=409, detail="A failed booking is required before re-planning.")
    if booking.get("verification_outcome") != "failed":
        raise HTTPException(status_code=409, detail="Re-planning is only available after failed verification.")
    drift = json.loads(booking.get("drift_json") or "[]")
    if not drift:
        raise HTTPException(status_code=409, detail="Failed verification did not record recycler drift.")

    excluded_centres: set[str] = set()
    for change in drift:
        centre_name = change.get("centre")
        if not centre_name or centre_name == "booking":
            continue
        seed = next(
            (
                entry for entry in SEED_RECYCLERS
                if centre_name in {entry["id"], entry["name"], entry["slug"]}
            ),
            None,
        )
        if seed:
            excluded_centres.update({seed["id"], seed["name"], seed["slug"]})
    if not excluded_centres:
        raise HTTPException(status_code=409, detail="No failed recycler centre can be excluded from the replacement plan.")

    items = json.loads(original.get("items_json") or "[]")
    if not items:
        items = [{"item_type": original["item_type"], "quantity": original["quantity"]}]
    replacement_id = "EW-" + uuid.uuid4().hex[:8].upper()
    coordinator = BoundedAgentCoordinator(
        request_id=replacement_id,
        app_instance=app,
        excluded_centres=excluded_centres,
        request_base_url=str(request.base_url).rstrip("/"),
    )
    agent_result = coordinator.run_agent_workflow(
        items=items,
        city=original.get("city") or original["location"],
        pincode=original.get("pincode") or "",
        max_fee=original["max_fee"],
    )
    if agent_result.get("status") == "failed" and agent_result.get("plan") is None:
        raise HTTPException(status_code=422, detail=agent_result.get("reason", "Replacement plan validation failed."))
    plan = agent_result["plan"]
    plan["replaces_request_id"] = request_id
    timestamp = now_iso()
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO requests(
                id, item_type, quantity, location, city, pincode, max_fee, items_json,
                replaces_request_id, status, plan_json, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                replacement_id,
                items[0]["item_type"],
                items[0]["quantity"],
                original["location"],
                original.get("city"),
                original.get("pincode"),
                original["max_fee"],
                json.dumps(items),
                request_id,
                plan["status"],
                json.dumps(plan),
                timestamp,
                timestamp,
            ),
        )
        replacement = fetchone_dict(conn.execute(
            "SELECT * FROM requests WHERE id = ?", (replacement_id,)
        ))
    return request_row(replacement)


# ---------------------------------------------------------------------------
# Official Scenarios & /tests API
# ---------------------------------------------------------------------------

@app.get("/api/tests")
def get_test_runs() -> list[dict[str, Any]]:
    with db_connection() as conn:
        cursor = conn.execute("SELECT * FROM test_runs ORDER BY id DESC LIMIT 50")
        rows = fetchall_dict(cursor)
    return sanitize_collected_status([
        {
            **r,
            "input": json.loads(r["input_json"]) if r.get("input_json") else {},
            "evidence": json.loads(r["evidence_json"]) if r.get("evidence_json") else {},
        }
        for r in rows
    ])


@app.post("/api/tests/run")
def run_official_tests() -> dict[str, Any]:
    """Runs official test cases TC1–TC4 and the separately labelled TC5 bonus."""
    cases = ["TC1", "TC2", "TC3", "TC4", "TC5"]
    results = [run_single_test_case(c) for c in cases]
    return {
        "total": len(results),
        "official_total": 4,
        "bonus_total": 1,
        "passed": sum(1 for r in results if r["passed"]),
        "results": results,
        "note": "Scenarios use the in-process API and synthetic mock recycler sources. No collected status exists.",
    }


@app.post("/api/tests/run/{case_id}")
def run_single_test(case_id: str) -> dict[str, Any]:
    case_upper = case_id.strip().upper()
    if case_upper not in ("TC1", "TC2", "TC3", "TC4", "TC5"):
        raise HTTPException(status_code=400, detail="Invalid test case ID. Choose from TC1, TC2, TC3, TC4, TC5.")
    return run_single_test_case(case_upper)


def run_single_test_case(case_id: str) -> dict[str, Any]:
    """Executes one benchmark through the real in-process API and persists the result."""
    reset_demo_scenarios()
    case_inputs = {
        "TC1": {
            "item_type": "laptop", "quantity": 1, "city": "Mysuru",
            "pincode": "570001", "max_fee": 100.0,
        },
        "TC2": {
            "items": [
                {"item_type": "paper", "quantity": 1},
                {"item_type": "battery", "quantity": 1},
            ],
            "city": "Mysuru", "pincode": "570001", "max_fee": 250.0,
        },
        "TC3": {
            "item_type": "laptop", "quantity": 1, "city": "Mysuru",
            "pincode": "570001", "max_fee": 10.0,
        },
        "TC4": {
            "item_type": "laptop", "quantity": 1, "city": "Mysuru",
            "pincode": "570001", "max_fee": 100.0,
        },
        "TC5": {
            "item_type": "laptop", "quantity": 1, "city": "Mysuru",
            "pincode": "570001", "max_fee": 100.0,
            "scenario": {"slug": "greenloop", "scenario": "fee_up"},
        },
    }
    expectations = {
        "TC1": "API creates a draft with fetched laptop acceptance evidence, source URL, and retrieval timestamp.",
        "TC2": "API evaluates paper and battery independently; fetched PaperBack HTML accepts paper and rejects battery. A rejected or unresolved item must not yield an accepted draft.",
        "TC3": "Booking is blocked before approval; fee-exception approval is required; explicitly approved request can be booked.",
        "TC4": "API creates, approves, books, and verifies a simulated booking; verification is not collection and status never becomes COLLECTED.",
        "TC5": "Bonus: simulate a live fee change after approval, detect and persist drift, then create an unbooked replacement plan that excludes the failed centre and requires fresh approval.",
    }
    inp = case_inputs.get(case_id, {})
    expected = expectations.get(case_id, "A supported benchmark case executes through the API.")
    actual = "Scenario did not complete."
    evidence: dict[str, Any] = {}
    explanation = "Scenario execution uses the in-process API and synthetic HTML evidence sources."
    passed = False

    async def execute_api_scenario() -> None:
        nonlocal actual, evidence, explanation, passed

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://benchmark.local",
        ) as api_client:
            async def call(method: str, path: str, payload: Optional[dict[str, Any]] = None):
                response = await api_client.request(method, path, json=payload)
                try:
                    body = response.json()
                except ValueError:
                    body = {"detail": response.text}
                return response.status_code, body

            create_status, created = await call("POST", "/api/requests", inp)
            if create_status != 200:
                actual = f"Request creation returned HTTP {create_status}: {created}"
                evidence = {"create_status_code": create_status, "response": created}
                return
            request_id = created["id"]
            plan = created["plan"]

            if case_id == "TC1":
                selected = plan.get("selected_recycler") or {}
                laptop = next(
                    (item for item in plan.get("items", []) if item.get("item_type") == "laptop"),
                    {},
                )
                acceptance = any(
                    record.get("decision") == "accepted"
                    and record.get("name") == selected.get("recycler_name")
                    for record in laptop.get("recycler_evidence", [])
                )
                passed = (
                    created["status"] == "draft"
                    and selected.get("recycler_id") == "mock-recycler-a"
                    and acceptance
                    and bool(selected.get("source_url"))
                    and bool(selected.get("retrieved_at"))
                )
                actual = (
                    f"HTTP {create_status}; request {created['status']}; "
                    f"selected {selected.get('recycler_name')}; acceptance evidence present: {acceptance}."
                )
                evidence = {
                    "request_id": request_id,
                    "request_status": created["status"],
                    "item_decision": laptop.get("decision"),
                    "selected_recycler": selected.get("recycler_name"),
                    "source_url": selected.get("source_url"),
                    "retrieved_at": selected.get("retrieved_at"),
                    "acceptance_evidence": acceptance,
                }
                explanation = "The request endpoint returned a draft and its fetched GreenLoop HTML evidence lists laptop as accepted."

            elif case_id == "TC2":
                plan_items = {item.get("item_type"): item for item in plan.get("items", [])}
                paper_item = plan_items.get("paper", {})
                battery_item = plan_items.get("battery", {})
                paperback_paper = next(
                    (
                        record for record in paper_item.get("recycler_evidence", [])
                        if record.get("name") == "PaperBack Materials Centre"
                    ),
                    {},
                )
                paperback_battery = next(
                    (
                        record for record in battery_item.get("recycler_evidence", [])
                        if record.get("name") == "PaperBack Materials Centre"
                    ),
                    {},
                )
                mixed_item_guard = all(
                    item.get("decision") == "accepted" or plan.get("status") != "draft"
                    for item in plan_items.values()
                )
                passed = (
                    paper_item.get("decision") == "accepted"
                    and paperback_paper.get("decision") == "accepted"
                    and paperback_battery.get("decision") == "rejected"
                    and bool(paperback_paper.get("source_url"))
                    and bool(paperback_paper.get("retrieved_at"))
                    and bool(paperback_battery.get("source_url"))
                    and bool(paperback_battery.get("retrieved_at"))
                    and mixed_item_guard
                )
                actual = (
                    f"paper item: {paper_item.get('decision')}; battery item: {battery_item.get('decision')}; "
                    f"PaperBack evidence: paper {paperback_paper.get('decision')}, "
                    f"battery {paperback_battery.get('decision')}; request plan: {plan.get('status')}."
                )
                evidence = {
                    "request_id": request_id,
                    "request_status": created["status"],
                    "plan_status": plan.get("status"),
                    "items": {
                        key: {
                            "decision": value.get("decision"),
                            "quantity": value.get("quantity"),
                        }
                        for key, value in plan_items.items()
                    },
                    "paperback_paper": paperback_paper,
                    "paperback_battery": paperback_battery,
                    "mixed_item_aggregate_guard": mixed_item_guard,
                }
                explanation = (
                    "The API's per-item outcomes are recorded separately from recycler-specific evidence. "
                    "PaperBack's battery rejection does not override acceptance evidence from other recyclers; "
                    "the aggregate guard prevents a draft if an item is actually rejected or unresolved."
                )

            elif case_id == "TC3":
                before_approval_status, before_approval = await call(
                    "POST", f"/api/requests/{request_id}/book"
                )
                no_exception_status, no_exception = await call(
                    "POST",
                    f"/api/requests/{request_id}/approval",
                    {"approved": True, "accept_fee_exception": False},
                )
                exception_status, exception_approval = await call(
                    "POST",
                    f"/api/requests/{request_id}/approval",
                    {"approved": True, "accept_fee_exception": True},
                )
                booking_status, booking = await call(
                    "POST", f"/api/requests/{request_id}/book"
                )
                passed = (
                    created["status"] == "awaiting_approval"
                    and plan.get("requires_fee_exception") is True
                    and plan.get("total_fee", 0) > inp["max_fee"]
                    and before_approval_status == 409
                    and "approval" in str(before_approval.get("detail", "")).lower()
                    and no_exception_status == 409
                    and "fee exception" in str(no_exception.get("detail", "")).lower()
                    and exception_status == 200
                    and exception_approval.get("status") == "approved"
                    and booking_status == 200
                    and booking.get("status") == "requested"
                )
                actual = (
                    f"request {created['status']}; booking before approval HTTP {before_approval_status}; "
                    f"approval without fee exception HTTP {no_exception_status}; "
                    f"explicit fee exception approval HTTP {exception_status}; "
                    f"booking after approval HTTP {booking_status} ({booking.get('status')})."
                )
                evidence = {
                    "request_id": request_id,
                    "proposed_fee": plan.get("total_fee"),
                    "user_fee_limit": inp["max_fee"],
                    "booking_before_approval": {
                        "status_code": before_approval_status,
                        "response": before_approval,
                    },
                    "approval_without_fee_exception": {
                        "status_code": no_exception_status,
                        "response": no_exception,
                    },
                    "approval_with_fee_exception": {
                        "status_code": exception_status,
                        "response_status": exception_approval.get("status"),
                    },
                    "booking_after_approval": {
                        "status_code": booking_status,
                        "status": booking.get("status"),
                        "reference": booking.get("reference"),
                    },
                }
                explanation = "The actual API rejects both pre-approval booking and approval without explicit fee-exception consent; consent allows approval and booking."

            elif case_id == "TC4":
                approval_status, approval = await call(
                    "POST",
                    f"/api/requests/{request_id}/approval",
                    {"approved": True, "accept_fee_exception": False},
                )
                booking_status, booking = await call(
                    "POST", f"/api/requests/{request_id}/book"
                )
                verification_status, verification = await call(
                    "POST", f"/api/requests/{request_id}/verify"
                )
                request_status, final_request = await call(
                    "GET", f"/api/requests/{request_id}"
                )
                with db_connection() as conn:
                    request_row_status = fetchone_dict(conn.execute(
                        "SELECT status FROM requests WHERE id = ?", (request_id,)
                    ))
                    booking_row_status = fetchone_dict(conn.execute(
                        "SELECT status FROM bookings WHERE request_id = ?", (request_id,)
                    ))
                persisted_statuses = [
                    request_row_status["status"] if request_row_status else None,
                    booking_row_status["status"] if booking_row_status else None,
                ]
                returned_statuses = [
                    created.get("status"),
                    approval.get("status"),
                    booking.get("status"),
                    verification.get("status"),
                    final_request.get("status"),
                    (final_request.get("booking") or {}).get("status"),
                ]
                no_collected_status = all(
                    not isinstance(status, str) or status.casefold() != "collected"
                    for status in persisted_statuses + returned_statuses
                )
                wording = verification.get("display_status")
                passed = (
                    approval_status == 200
                    and approval.get("status") == "approved"
                    and booking_status == 200
                    and booking.get("status") == "requested"
                    and verification_status == 200
                    and verification.get("verified") is True
                    and verification.get("status") == "verified"
                    and verification.get("collection_confirmed") is False
                    and "booking reference" in str(verification.get("evidence", "")).lower()
                    and wording == "Requested, not yet collected."
                    and request_status == 200
                    and no_collected_status
                )
                actual = (
                    f"approval HTTP {approval_status}; booking HTTP {booking_status} "
                    f"({booking.get('status')}); verification HTTP {verification_status} "
                    f"(verified: {verification.get('verified')}, collection confirmed: "
                    f"{verification.get('collection_confirmed')}); stored statuses: {persisted_statuses}."
                )
                evidence = {
                    "request_id": request_id,
                    "booking_reference": booking.get("reference"),
                    "verification_evidence": verification.get("evidence"),
                    "verification_status": verification.get("status"),
                    "verified": verification.get("verified"),
                    "collection_confirmed": verification.get("collection_confirmed"),
                    "display_wording": wording,
                    "persisted_statuses": persisted_statuses,
                    "returned_statuses": returned_statuses,
                    "no_collected_status": no_collected_status,
                }
                explanation = "The API create/approve/book/verify workflow validates simulated booking evidence; neither returned nor stored lifecycle status claims physical collection."

            elif case_id == "TC5":
                approval_status, approval = await call(
                    "POST", f"/api/requests/{request_id}/approval", {"approved": True}
                )
                booking_status, booking = await call(
                    "POST", f"/api/requests/{request_id}/book"
                )
                scenario_status, scenario_result = await call(
                    "POST", "/api/demo/scenario", inp["scenario"]
                )
                verify_status, verification = await call(
                    "POST", f"/api/requests/{request_id}/verify"
                )
                replan_status, replacement = await call(
                    "POST", f"/api/requests/{request_id}/replan"
                )
                failed_centre_ids = {
                    seed["id"] for seed in SEED_RECYCLERS
                    if any(
                        row.get("centre") in {seed["id"], seed["name"], seed["slug"]}
                        for row in verification.get("drift", [])
                    )
                }
                replacement_centres = {
                    centre.get("recycler_id")
                    for centre in replacement.get("plan", {}).get("centres", [])
                }
                passed = (
                    approval_status == 200
                    and booking_status == 200
                    and scenario_status == 200
                    and verify_status == 200
                    and verification.get("status") == "failed"
                    and any(row.get("field") == "fee" for row in verification.get("drift", []))
                    and replan_status == 200
                    and bool(failed_centre_ids)
                    and not replacement_centres.intersection(failed_centre_ids)
                    and replacement.get("approval") is None
                    and replacement.get("booking") is None
                    and replacement.get("status") in {"draft", "awaiting_approval"}
                )
                actual = (
                    f"Original approval HTTP {approval_status}, booking HTTP {booking_status}; "
                    f"verification {verification.get('status')} with {len(verification.get('drift', []))} drift item(s); "
                    f"re-plan HTTP {replan_status}, new status {replacement.get('status')}."
                )
                evidence = {
                    "request_id": request_id,
                    "booking_reference": booking.get("reference"),
                    "scenario": scenario_result,
                    "verification": verification,
                    "replacement_request_id": replacement.get("id"),
                    "replacement_status": replacement.get("status"),
                    "failed_centre_ids": sorted(failed_centre_ids),
                    "replacement_centres": sorted(replacement_centres),
                    "replacement_has_no_approval_or_booking": (
                        replacement.get("approval") is None
                        and replacement.get("booking") is None
                    ),
                }
                explanation = (
                    "TC5 is a bonus demo: a synthetic live profile changes after booking, verification reports exact drift, "
                    "and the agent proposes a replacement excluding that centre without approving or booking it."
                )

    try:
        if case_id not in case_inputs:
            raise ValueError(f"Unknown benchmark case: {case_id}")
        asyncio.run(execute_api_scenario())
    except Exception as exc:
        logger.exception("Benchmark scenario %s failed during execution", case_id)
        actual = f"Scenario execution error: {type(exc).__name__}: {exc}"
        evidence = {"error_type": type(exc).__name__, "error": str(exc)}
        passed = False

    # Persist test run record.
    timestamp = now_iso()
    try:
        with db_connection() as conn:
            conn.execute(
                """
                INSERT INTO test_runs(case_id, input_json, expected, actual, passed, evidence_json, explanation, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (case_id, json.dumps(inp), expected, actual, int(passed), json.dumps(evidence), explanation, timestamp),
            )
    except Exception as exc:
        logger.warning("Could not persist test run for %s: %s", case_id, exc)

    return {
        "case_id": case_id,
        "input": inp,
        "expected": expected,
        "actual": actual,
        "passed": passed,
        "evidence": evidence,
        "explanation": explanation,
        "timestamp": timestamp,
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
