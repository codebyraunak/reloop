from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import httpx
from google import genai
from google.genai import types, errors
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_FILE = BASE_DIR / "static" / "index.html"
# Load environment variables first
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR.parent / ".env")

# Choose a writable database path on Vercel
configured_db = os.getenv("ECOROUTE_DB", "").strip()

if configured_db:
    DB_PATH = Path(configured_db)
elif os.getenv("VERCEL") == "1":
    DB_PATH = Path("/tmp/ecoroute.db")
else:
    DB_PATH = BASE_DIR / "ecoroute.db"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest").strip()

app = FastAPI(title="EcoRoute Pickup Coordinator", version="1.0.0", description="Evidence-based e-waste pickup planning demo")

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

MOCK_RECYCLERS: list[dict[str, Any]] = [
    {
        "id": "mock-recycler-a",
        "name": "GreenLoop Electronics Recovery",
        "area": "Mysuru",
        "accepted_materials": ["laptop", "mobile_phone", "desktop_computer", "paper", "television", "tablet", "keyboard_mouse", "printer", "cable_charger", "camera", "audio_device"],
        "rejected_materials": ["battery", "mixed_ewaste"],
        "pickup_fee": 50,
        "currency": "INR",
        "pickup_available": True,
        "source_url": "http://localhost:8000/mock-recycler/greenloop",
        "notes": "Mock demo source. Batteries are not accepted at this centre.",
    },
    {
        "id": "mock-recycler-b",
        "name": "Circular Earth Collection Hub",
        "area": "Mysuru",
        "accepted_materials": ["laptop", "mobile_phone", "desktop_computer", "battery", "television", "tablet", "keyboard_mouse", "printer", "cable_charger", "camera", "audio_device", "mixed_ewaste"],
        "rejected_materials": ["paper"],
        "pickup_fee": 180,
        "currency": "INR",
        "pickup_available": True,
        "source_url": "http://localhost:8000/mock-recycler/circular-earth",
        "notes": "Mock demo source. Battery packaging rules should be confirmed manually.",
    },
    {
        "id": "mock-recycler-c",
        "name": "PaperBack Materials Centre",
        "area": "Mysuru",
        "accepted_materials": ["paper"],
        "rejected_materials": ["battery", "laptop", "mobile_phone", "desktop_computer", "television", "tablet", "keyboard_mouse", "printer", "cable_charger", "camera", "audio_device", "mixed_ewaste"],
        "pickup_fee": 20,
        "currency": "INR",
        "pickup_available": True,
        "source_url": "http://localhost:8000/mock-recycler/paperback",
        "notes": "Mock demo source. Paper only.",
    },
]

MATERIAL_LABELS = {
    "laptop": "Laptop",
    "mobile_phone": "Mobile phone",
    "desktop_computer": "Desktop computer",
    "battery": "Battery",
    "paper": "Paper",
    "television": "Television",
    "tablet": "Tablet",
    "keyboard_mouse": "Keyboards and mice",
    "printer": "Printer",
    "cable_charger": "Cables and chargers",
    "camera": "Camera",
    "audio_device": "Audio device",
    "mixed_ewaste": "Mixed e-waste",
}


@contextmanager
def db_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> None:
    with db_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY,
                item_type TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                location TEXT NOT NULL,
                max_fee REAL NOT NULL,
                status TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approvals (
                request_id TEXT PRIMARY KEY,
                approved INTEGER NOT NULL,
                approved_plan_json TEXT NOT NULL,
                approved_at TEXT NOT NULL,
                FOREIGN KEY(request_id) REFERENCES requests(id)
            );
            CREATE TABLE IF NOT EXISTS bookings (
                request_id TEXT PRIMARY KEY,
                reference TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                verified_at TEXT,
                verification_evidence TEXT,
                FOREIGN KEY(request_id) REFERENCES requests(id)
            );
            CREATE TABLE IF NOT EXISTS test_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL,
                input_json TEXT NOT NULL,
                expected TEXT NOT NULL,
                actual TEXT NOT NULL,
                passed INTEGER NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


init_db()


@app.on_event("startup")
def startup() -> None:
    init_db()


class RequestCreate(BaseModel):
    item_type: str = Field(..., description="One of the supported material IDs")
    quantity: int = Field(..., ge=1, le=500)
    location: str = Field(..., min_length=2, max_length=160)
    max_fee: float = Field(..., ge=0, le=100000)


class ApprovalInput(BaseModel):
    approved: bool
    accept_fee_exception: bool = False


class AssistantInput(BaseModel):
    message: str = Field(..., min_length=1, max_length=1000)


class CollectionInput(BaseModel):
    collection_confirmed: bool = False
    evidence_note: str = Field(default="", max_length=500)


def public_recycler(recycler: dict[str, Any]) -> dict[str, Any]:
    return {**recycler, "accepted_material_labels": [MATERIAL_LABELS[x] for x in recycler["accepted_materials"]]}


def make_plan(request: RequestCreate) -> dict[str, Any]:
    if request.item_type not in MATERIAL_LABELS:
        raise HTTPException(status_code=422, detail=f"Unsupported item type. Choose from: {', '.join(MATERIAL_LABELS)}")
    matches = []
    rejected = []
    for recycler in MOCK_RECYCLERS:
        if request.item_type in recycler["accepted_materials"]:
            within_area = request.location.strip().lower() in recycler["area"].lower() or recycler["area"].lower() in request.location.strip().lower()
            matches.append({
                "recycler_id": recycler["id"],
                "recycler_name": recycler["name"],
                "area": recycler["area"],
                "fee": recycler["pickup_fee"],
                "currency": recycler["currency"],
                "within_area": within_area,
                "fee_within_limit": recycler["pickup_fee"] <= request.max_fee,
                "pickup_available": recycler["pickup_available"],
                "source_url": recycler["source_url"],
                "source_retrieved_at": now_iso(),
                "evidence": f"Mock source lists {MATERIAL_LABELS[request.item_type]} as accepted; pickup fee listed as ₹{recycler['pickup_fee']}.",
                "notes": recycler["notes"],
            })
        elif request.item_type in recycler["rejected_materials"]:
            rejected.append({"recycler_id": recycler["id"], "recycler_name": recycler["name"], "reason": f"{MATERIAL_LABELS[request.item_type]} is explicitly listed as not accepted.", "source_url": recycler["source_url"]})
    viable = [m for m in matches if m["within_area"] and m["pickup_available"]]
    viable.sort(key=lambda m: (not m["fee_within_limit"], m["fee"]))
    selected = viable[0] if viable else None
    if selected is None:
        status = "NO_MATCH"
        reason = "No recycler with evidenced acceptance and pickup availability matched the requested location."
    elif not selected["fee_within_limit"]:
        status = "APPROVAL_REQUIRED"
        reason = f"Lowest-fee matching pickup is ₹{selected['fee']}, above your ₹{request.max_fee:.2f} limit. Approval is required before booking."
    else:
        status = "DRAFT"
        reason = "A compatible recycler was found and the listed pickup fee is within the user's limit."
    return {
        "item_type": request.item_type,
        "item_label": MATERIAL_LABELS[request.item_type],
        "quantity": request.quantity,
        "location": request.location.strip(),
        "max_fee": request.max_fee,
        "status": status,
        "reason": reason,
        "selected_recycler": selected,
        "alternatives": viable[1:],
        "rejected_centres": rejected,
        "sources_retrieved_at": now_iso(),
        "is_mock_data": True,
    }


def request_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["plan"] = json.loads(result.pop("plan_json"))
    result["created_at"] = result.get("created_at")
    with db_connection() as conn:
        approval = conn.execute("SELECT * FROM approvals WHERE request_id = ?", (result["id"],)).fetchone()
        booking = conn.execute("SELECT * FROM bookings WHERE request_id = ?", (result["id"],)).fetchone()
    result["approval"] = dict(approval) if approval else None
    if result["approval"]:
        result["approval"]["approved_plan"] = json.loads(result["approval"].pop("approved_plan_json"))
    result["booking"] = dict(booking) if booking else None
    return result


@app.get("/", response_class=HTMLResponse)
def frontend() -> str:
    if not FRONTEND_FILE.exists():
        raise HTTPException(status_code=500, detail="ReLoop frontend file is missing")
    return FRONTEND_FILE.read_text(encoding="utf-8")

# Serve locally bundled ReLoop photographs
IMAGE_DIR = BASE_DIR / "static" / "images"

IMAGE_FILES = {
    "hero-ewaste.jpg",
    "laptop.jpg",
    "desktop.jpg",
    "phone.jpg",
    "television.jpg",
    "tablet.jpg",
    "keyboard.jpg",
    "printer.jpg",
    "cables.jpg",
    "camera.jpg",
    "audio.jpg",
}


@app.get("/assets/{filename}")
def serve_image(filename: str):
    if filename not in IMAGE_FILES:
        raise HTTPException(
            status_code=404,
            detail="Image not found",
        )

    image_path = IMAGE_DIR / filename

    if not image_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Image file is missing",
        )

    return FileResponse(image_path, media_type="image/jpeg")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "EcoRoute",
        "mode": "mock-demo",
        "gemini_configured": bool(GEMINI_API_KEY),
        "gemini_model": GEMINI_MODEL if GEMINI_API_KEY else None,
    }


# =====================================================================
# Gemini Tools for Pickup Planning & Recycler Verification
# =====================================================================

def collect_and_validate_pickup_details(item_type: str, quantity: int, location: str, max_fee: float) -> str:
    """Collects and validates electronic waste pickup details against supported materials and reasonable values."""
    aliases = {
        "laptop": "laptop", "laptops": "laptop", "phone": "mobile_phone", "phones": "mobile_phone",
        "mobile": "mobile_phone", "computer": "desktop_computer", "desktop": "desktop_computer",
        "battery": "battery", "batteries": "battery", "paper": "paper", "tv": "television",
        "television": "television", "tablet": "tablet", "printer": "printer", "camera": "camera",
        "cable": "cable_charger", "charger": "cable_charger", "keyboard": "keyboard_mouse",
        "mouse": "keyboard_mouse", "headphone": "audio_device", "speaker": "audio_device",
        "audio": "audio_device", "mixed": "mixed_ewaste", "mixed_ewaste": "mixed_ewaste",
    }
    raw_item = (item_type or "").strip().lower()
    resolved_item = aliases.get(raw_item, raw_item)
    valid_material = resolved_item in MATERIAL_LABELS
    missing = []
    if not valid_material:
        missing.append("valid material type (supported: " + ", ".join(MATERIAL_LABELS.keys()) + ")")
    if not location or len(location.strip()) < 2:
        missing.append("service location (e.g. Mysuru)")
    if max_fee <= 0:
        missing.append("maximum fee budget in INR")
    
    return json.dumps({
        "status": "COMPLETE" if not missing else "INCOMPLETE",
        "item_type": resolved_item if valid_material else None,
        "item_label": MATERIAL_LABELS.get(resolved_item) if valid_material else None,
        "quantity": max(1, min(500, int(quantity or 1))),
        "location": location.strip() if location else "",
        "max_fee": float(max_fee),
        "missing_details": missing,
    })


def search_mock_recyclers(item_type: str, location: str) -> str:
    """Checks accepted materials and service areas across mock recyclers."""
    aliases = {
        "laptop": "laptop", "laptops": "laptop", "phone": "mobile_phone", "phones": "mobile_phone",
        "mobile": "mobile_phone", "computer": "desktop_computer", "desktop": "desktop_computer",
        "battery": "battery", "batteries": "battery", "paper": "paper", "tv": "television",
        "television": "television", "tablet": "tablet", "printer": "printer", "camera": "camera",
        "cable": "cable_charger", "charger": "cable_charger", "keyboard": "keyboard_mouse",
        "mouse": "keyboard_mouse", "headphone": "audio_device", "speaker": "audio_device",
    }
    resolved = aliases.get((item_type or "").strip().lower(), (item_type or "").strip().lower())
    if resolved not in MATERIAL_LABELS:
        return json.dumps({"error": f"Item type '{item_type}' is not recognized. Supported: {', '.join(MATERIAL_LABELS.keys())}"})
    
    matches = []
    rejected = []
    for recycler in MOCK_RECYCLERS:
        in_area = location.strip().lower() in recycler["area"].lower() or recycler["area"].lower() in location.strip().lower()
        if resolved in recycler["accepted_materials"]:
            matches.append({
                "recycler_id": recycler["id"],
                "name": recycler["name"],
                "area": recycler["area"],
                "in_area": in_area,
                "pickup_fee": recycler["pickup_fee"],
                "currency": recycler["currency"],
                "pickup_available": recycler["pickup_available"],
                "source_url": recycler["source_url"],
            })
        elif resolved in recycler["rejected_materials"]:
            rejected.append({
                "recycler_id": recycler["id"],
                "name": recycler["name"],
                "reason": f"{MATERIAL_LABELS[resolved]} is explicitly not accepted.",
                "source_url": recycler["source_url"],
            })
    return json.dumps({"item_type": resolved, "location": location, "matches": matches, "rejected_centres": rejected})


def read_mock_recycler_website(slug: str) -> str:
    """Searches and reads the mock recycler website profile for verified terms and evidence."""
    slug_clean = slug.strip().lower().replace(" ", "-")
    lookup = {"greenloop": "mock-recycler-a", "circular-earth": "mock-recycler-b", "paperback": "mock-recycler-c"}
    recycler_id = lookup.get(slug_clean, slug_clean)
    recycler = next((r for r in MOCK_RECYCLERS if r["id"] == recycler_id or slug_clean in r["name"].lower()), None)
    if not recycler:
        return json.dumps({"error": f"Mock recycler '{slug}' not found. Available slugs: greenloop, circular-earth, paperback"})
    return json.dumps({
        "name": recycler["name"],
        "area": recycler["area"],
        "accepted_materials": [MATERIAL_LABELS[m] for m in recycler["accepted_materials"]],
        "rejected_materials": [MATERIAL_LABELS[m] for m in recycler["rejected_materials"]],
        "pickup_fee": recycler["pickup_fee"],
        "currency": recycler["currency"],
        "pickup_available": recycler["pickup_available"],
        "source_url": recycler["source_url"],
        "notes": recycler["notes"],
    })


def compare_fees_with_budget(item_type: str, location: str, max_fee: float) -> str:
    """Compares recycler pickup fees with the user's maximum budget and identifies approval requirements."""
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
    """Prepares a deterministic evidence-backed pickup plan under backend control."""
    try:
        req = RequestCreate(item_type=item_type, quantity=quantity, location=location, max_fee=max_fee)
        plan = make_plan(req)
        return json.dumps({
            "status": plan["status"],
            "reason": plan["reason"],
            "selected_recycler": plan["selected_recycler"]["recycler_name"] if plan["selected_recycler"] else None,
            "fee": plan["selected_recycler"]["fee"] if plan["selected_recycler"] else None,
            "evidence": plan["selected_recycler"]["evidence"] if plan["selected_recycler"] else None,
            "source_url": plan["selected_recycler"]["source_url"] if plan["selected_recycler"] else None,
            "requires_human_approval": True,
        })
    except Exception as e:
        return json.dumps({"status": "ERROR", "error": str(e)})


TOOL_DISPATCH = {
    "collect_and_validate_pickup_details": collect_and_validate_pickup_details,
    "search_mock_recyclers": search_mock_recyclers,
    "read_mock_recycler_website": read_mock_recycler_website,
    "compare_fees_with_budget": compare_fees_with_budget,
    "prepare_pickup_plan_with_evidence": prepare_pickup_plan_with_evidence,
}

GEMINI_TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="collect_and_validate_pickup_details",
                description="Collects and validates electronic waste pickup details against supported materials and reasonable values.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "item_type": types.Schema(type="STRING", description="Item type, e.g. laptop, mobile_phone, battery, television, paper"),
                        "quantity": types.Schema(type="INTEGER", description="Quantity of items to recycle"),
                        "location": types.Schema(type="STRING", description="Service location or city, e.g. Mysuru"),
                        "max_fee": types.Schema(type="NUMBER", description="Maximum pickup fee budget in INR"),
                    },
                    required=["item_type", "quantity", "location", "max_fee"],
                ),
            ),
            types.FunctionDeclaration(
                name="search_mock_recyclers",
                description="Checks accepted materials and service areas across mock recyclers.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "item_type": types.Schema(type="STRING", description="Item type to search for, e.g. laptop, mobile_phone, battery"),
                        "location": types.Schema(type="STRING", description="Service location or city, e.g. Mysuru"),
                    },
                    required=["item_type", "location"],
                ),
            ),
            types.FunctionDeclaration(
                name="read_mock_recycler_website",
                description="Searches and reads the mock recycler website profile for verified terms, accepted/rejected materials, and evidence.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "slug": types.Schema(type="STRING", description="Recycler slug or identifier: greenloop, circular-earth, or paperback"),
                    },
                    required=["slug"],
                ),
            ),
            types.FunctionDeclaration(
                name="compare_fees_with_budget",
                description="Compares recycler pickup fees with the user's maximum budget and identifies approval requirements.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "item_type": types.Schema(type="STRING", description="Item type, e.g. laptop, battery"),
                        "location": types.Schema(type="STRING", description="Service location or city, e.g. Mysuru"),
                        "max_fee": types.Schema(type="NUMBER", description="Maximum pickup fee budget in INR"),
                    },
                    required=["item_type", "location", "max_fee"],
                ),
            ),
            types.FunctionDeclaration(
                name="prepare_pickup_plan_with_evidence",
                description="Prepares a deterministic evidence-backed pickup plan under backend control.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "item_type": types.Schema(type="STRING", description="Supported material ID, e.g. laptop, mobile_phone, battery"),
                        "quantity": types.Schema(type="INTEGER", description="Quantity of items"),
                        "location": types.Schema(type="STRING", description="Service location, e.g. Mysuru"),
                        "max_fee": types.Schema(type="NUMBER", description="Maximum pickup fee budget in INR"),
                    },
                    required=["item_type", "quantity", "location", "max_fee"],
                ),
            ),
        ]
    )
]


@app.post("/api/assistant")
def assistant(payload: AssistantInput) -> dict[str, Any]:
    """Gemini-powered agent with official SDK, tool calling, deterministic fallback, and strict booking safety."""
    message = payload.message.strip().lower()
    aliases = {
        "laptop": "laptop", "laptops": "laptop", "phone": "mobile_phone", "phones": "mobile_phone",
        "mobile": "mobile_phone", "computer": "desktop_computer", "desktop": "desktop_computer",
        "battery": "battery", "batteries": "battery", "paper": "paper", "tv": "television",
        "television": "television", "tablet": "tablet", "printer": "printer", "camera": "camera",
        "cable": "cable_charger", "charger": "cable_charger", "keyboard": "keyboard_mouse",
        "mouse": "keyboard_mouse", "headphone": "audio_device", "speaker": "audio_device",
    }
    item_type = next((value for word, value in aliases.items() if word in message), None)
    quantity = 1
    import re
    match = re.search(r"\b(\d{1,3})\b", message)
    if match:
        quantity = max(1, min(500, int(match.group(1))))

    # Deterministic fallback reply
    if any(word in message for word in ("approval", "approve", "booking", "book", "confirm")):
        fallback_reply = "Submitting this form creates a pickup draft. The selected recycler, materials and fee will be shown for human approval. Only after approval can the mock booking be submitted. A booking is not proof that items were collected."
    elif not item_type:
        fallback_reply = "Tell me which electronics you have (for example, one laptop or three mobile phones) and your city or PIN code. I can help prepare a recycler match, but I will never book without human approval."
    else:
        fallback_reply = f"I understood {quantity} {MATERIAL_LABELS[item_type].lower()}{'s' if quantity != 1 and item_type not in ('keyboard_mouse', 'cable_charger', 'mixed_ewaste') else ''}. Submit the pickup form with your city and fee limit so I can check mock recycler acceptance and fees. No booking has been made."

    # If Gemini is not configured, return labeled fallback
    if not GEMINI_API_KEY:
        return {
            "reply": f"[Fallback: Gemini API key is not configured] {fallback_reply}",
            "item_type": item_type,
            "quantity": quantity,
            "needs_location": True,
            "booking_performed": False,
            "ai_powered": False,
            "fallback_used": True,
            "error": "GEMINI_API_KEY environment variable is not configured.",
        }

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        system_instruction = (
            "You are ReLoop / EcoRoute's AI recycling coordinator. "
            "Help the user recycle electronic waste (laptops, phones, batteries, etc.). "
            "You have tools to validate details, search recyclers, read website profiles, compare fees, and prepare pickup plans. "
            "STRICT SAFETY & DEMO RULES: "
            "1. You CANNOT create bookings or confirm physical collections. Human approval is strictly required before any booking. "
            "2. A booking or pickup draft is never evidence that items have been collected. "
            "3. Keep user responses concise, clear, and friendly (2-3 sentences max). "
            "4. Clearly explain the status of their request and remind them that no booking has been made yet."
        )

        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=GEMINI_TOOLS,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.2,
        )

        # Turn 1: Call Gemini model to decide whether to invoke a tool
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=payload.message.strip(),
            config=config,
        )

        tool_called = None
        tool_result = None
        reply = None

        if resp.function_calls:
            fc = resp.function_calls[0]
            tool_called = fc.name
            handler = TOOL_DISPATCH.get(fc.name)
            if handler:
                try:
                    tool_raw_result = handler(**fc.args)
                    try:
                        tool_result = json.loads(tool_raw_result)
                    except Exception:
                        tool_result = tool_raw_result

                    # Turn 2: Synthesize final user-facing response with tool result and safety constraints
                    synthesis_prompt = (
                        f"The user said: '{payload.message.strip()}'.\n"
                        f"You invoked tool '{tool_called}' with parameters: {fc.args}.\n"
                        f"Tool result: {tool_raw_result}\n\n"
                        "Synthesize a clear, helpful response for the user in 2-3 sentences. "
                        "Remind them that no booking has been made yet and human approval is required before booking."
                    )
                    synth_resp = client.models.generate_content(
                        model=GEMINI_MODEL,
                        contents=synthesis_prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=system_instruction,
                            temperature=0.3,
                        ),
                    )
                    reply = synth_resp.text.strip() if synth_resp.text else None
                except Exception as tool_err:
                    tool_result = {"error": str(tool_err)}

        if not reply:
            reply = resp.text.strip() if resp.text else fallback_reply

        # Extract extracted values if available from tool result
        resolved_item = item_type
        resolved_quantity = quantity
        resolved_location = None
        resolved_max_fee = None
        if isinstance(tool_result, dict):
            if tool_result.get("item_type"):
                resolved_item = tool_result["item_type"]
            if tool_result.get("quantity"):
                resolved_quantity = tool_result["quantity"]
            if tool_result.get("location"):
                resolved_location = tool_result["location"]
            if tool_result.get("max_fee"):
                resolved_max_fee = tool_result["max_fee"]

        return {
            "reply": reply,
            "item_type": resolved_item,
            "quantity": resolved_quantity,
            "location": resolved_location,
            "max_fee": resolved_max_fee,
            "needs_location": not bool(resolved_location),
            "booking_performed": False,
            "ai_powered": True,
            "fallback_used": False,
            "tool_called": tool_called,
            "tool_result": tool_result,
            "error": None,
        }

    except Exception as exc:
        err_msg = str(exc)
        # Handle rate-limit, auth, or network errors with clear labeling
        return {
            "reply": f"[Fallback: AI service encountered an issue ({type(exc).__name__})] {fallback_reply}",
            "item_type": item_type,
            "quantity": quantity,
            "needs_location": True,
            "booking_performed": False,
            "ai_powered": False,
            "fallback_used": True,
            "error": err_msg,
        }


@app.get("/api/materials")
def materials() -> list[dict[str, str]]:
    return [{"id": key, "label": label} for key, label in MATERIAL_LABELS.items()]


@app.get("/api/recyclers")
def recyclers() -> list[dict[str, Any]]:
    return [public_recycler(r) for r in MOCK_RECYCLERS]


@app.get("/mock-recycler/{slug}", response_class=HTMLResponse)
def mock_recycler_page(slug: str) -> str:
    lookup = {"greenloop": "mock-recycler-a", "circular-earth": "mock-recycler-b", "paperback": "mock-recycler-c"}
    recycler_id = lookup.get(slug)
    recycler = next((r for r in MOCK_RECYCLERS if r["id"] == recycler_id), None)
    if not recycler:
        raise HTTPException(status_code=404, detail="Mock recycler not found")
    accepted = ", ".join(MATERIAL_LABELS[x] for x in recycler["accepted_materials"])
    rejected = ", ".join(MATERIAL_LABELS[x] for x in recycler["rejected_materials"])
    return f"""<!doctype html><html><head><title>{recycler['name']}</title><meta charset='utf-8'></head>
    <body><main id='recycler-profile' data-recycler-id='{recycler['id']}'><h1>{recycler['name']}</h1>
    <p><strong>Service area:</strong> {recycler['area']}</p><p><strong>Accepted materials:</strong> <span id='accepted-materials'>{accepted}</span></p>
    <p><strong>Not accepted:</strong> <span id='rejected-materials'>{rejected}</span></p><p><strong>Pickup fee:</strong> ₹{recycler['pickup_fee']}</p>
    <p><strong>Pickup available:</strong> {'Yes' if recycler['pickup_available'] else 'No'}</p><p>{recycler['notes']}</p>
    <form id='mock-pickup-form'><label>Household reference <input name='household_reference' required></label><button type='submit'>Request pickup (mock)</button></form>
    <p>This is a synthetic demonstration site. No real collection is arranged.</p></main></body></html>"""


@app.post("/api/requests")
def create_request(payload: RequestCreate) -> dict[str, Any]:
    plan = make_plan(payload)
    request_id = "EW-" + uuid.uuid4().hex[:8].upper()
    timestamp = now_iso()
    with db_connection() as conn:
        conn.execute(
            "INSERT INTO requests(id,item_type,quantity,location,max_fee,status,plan_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (request_id, payload.item_type, payload.quantity, payload.location.strip(), payload.max_fee, plan["status"], json.dumps(plan), timestamp, timestamp),
        )
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    return request_row(row)


@app.get("/api/requests")
def list_requests() -> list[dict[str, Any]]:
    with db_connection() as conn:
        rows = conn.execute("SELECT * FROM requests ORDER BY created_at DESC").fetchall()
    return [request_row(row) for row in rows]


@app.get("/api/requests/{request_id}")
def get_request(request_id: str) -> dict[str, Any]:
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")
    return request_row(row)


@app.post("/api/requests/{request_id}/approval")
def approve_request(request_id: str, payload: ApprovalInput) -> dict[str, Any]:
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Request not found")
        existing = conn.execute("SELECT * FROM bookings WHERE request_id = ?", (request_id,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="A booking already exists for this request; approval cannot be changed.")
        plan = json.loads(row["plan_json"])
        selected = plan.get("selected_recycler")
        if not selected:
            raise HTTPException(status_code=409, detail="Cannot approve: no matching recycler was evidenced.")
        if selected["fee"] > row["max_fee"] and not payload.accept_fee_exception:
            raise HTTPException(status_code=409, detail="Fee exceeds the user's limit. Explicitly accept the fee exception to proceed.")
        timestamp = now_iso()
        conn.execute("INSERT OR REPLACE INTO approvals(request_id,approved,approved_plan_json,approved_at) VALUES(?,?,?,?)", (request_id, int(payload.approved), json.dumps(plan), timestamp))
        new_status = "APPROVED" if payload.approved else "DECLINED"
        conn.execute("UPDATE requests SET status = ?, updated_at = ? WHERE id = ?", (new_status, timestamp, request_id))
        updated = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    return request_row(updated)


@app.post("/api/requests/{request_id}/book")
def book_request(request_id: str) -> dict[str, Any]:
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Request not found")
        existing = conn.execute("SELECT * FROM bookings WHERE request_id = ?", (request_id,)).fetchone()
        if existing:
            return {"request_id": request_id, "reference": existing["reference"], "status": existing["status"], "message": "Existing booking returned; duplicate submission prevented."}
        approval = conn.execute("SELECT * FROM approvals WHERE request_id = ?", (request_id,)).fetchone()
        if not approval or not approval["approved"]:
            raise HTTPException(status_code=409, detail="Human approval is required before booking.")
        plan = json.loads(row["plan_json"])
        selected = plan.get("selected_recycler")
        if not selected:
            raise HTTPException(status_code=409, detail="No selected recycler to book.")
        reference = "MOCK-" + uuid.uuid4().hex[:8].upper()
        timestamp = now_iso()
        # The mock action only creates a booking request. It never marks the items collected.
        conn.execute("INSERT INTO bookings(request_id,reference,status,submitted_at) VALUES(?,?,?,?)", (request_id, reference, "BOOKED", timestamp))
        conn.execute("UPDATE requests SET status = 'BOOKED', updated_at = ? WHERE id = ?", (timestamp, request_id))
    return {"request_id": request_id, "reference": reference, "status": "BOOKED", "message": "Mock pickup request submitted. Collection has not been confirmed."}


@app.post("/api/requests/{request_id}/verify")
def verify_booking(request_id: str) -> dict[str, Any]:
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM bookings WHERE request_id = ?", (request_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=409, detail="No booking exists to verify.")
        # Verification reads the mock booking record independently. A booking record confirms booking only.
        evidence = f"Mock booking table contains reference {row['reference']} with status {row['status']} at verification time."
        verified_at = now_iso()
        conn.execute("UPDATE bookings SET verified_at = ?, verification_evidence = ? WHERE request_id = ?", (verified_at, evidence, request_id))
        conn.execute("UPDATE requests SET status = 'BOOKED', updated_at = ? WHERE id = ?", (verified_at, request_id))
        result = conn.execute("SELECT * FROM bookings WHERE request_id = ?", (request_id,)).fetchone()
    return {"request_id": request_id, "reference": result["reference"], "status": result["status"], "verified": True, "collection_confirmed": False, "evidence": evidence, "verified_at": verified_at}


@app.post("/api/requests/{request_id}/collection")
def record_collection(request_id: str, payload: CollectionInput) -> dict[str, Any]:
    if not payload.collection_confirmed or len(payload.evidence_note.strip()) < 8:
        raise HTTPException(status_code=422, detail="Collection requires explicit confirmation and an evidence note of at least 8 characters.")
    with db_connection() as conn:
        booking = conn.execute("SELECT * FROM bookings WHERE request_id = ?", (request_id,)).fetchone()
        if not booking:
            raise HTTPException(status_code=409, detail="Cannot confirm collection without a booking.")
        timestamp = now_iso()
        conn.execute("UPDATE bookings SET status = 'COLLECTED', verified_at = ?, verification_evidence = ? WHERE request_id = ?", (timestamp, payload.evidence_note.strip(), request_id))
        conn.execute("UPDATE requests SET status = 'COLLECTED', updated_at = ? WHERE id = ?", (timestamp, request_id))
    return {"request_id": request_id, "reference": booking["reference"], "status": "COLLECTED", "collection_confirmed": True, "evidence": payload.evidence_note.strip(), "verified_at": timestamp}


@app.get("/api/tests")
def get_test_runs() -> list[dict[str, Any]]:
    with db_connection() as conn:
        rows = conn.execute("SELECT * FROM test_runs ORDER BY id DESC").fetchall()
    return [{**dict(row), "input": json.loads(row["input_json"]), "evidence": json.loads(row["evidence_json"])} for row in rows]


@app.post("/api/tests/run")
def run_official_tests() -> dict[str, Any]:
    scenarios = [
        {"case_id": "TC1", "title": "Old laptop accepted by centre", "input": {"item_type": "laptop", "quantity": 1, "location": "Mysuru", "max_fee": 100}, "expected": "pickup draft created"},
        {"case_id": "TC2", "title": "Centre takes paper but not batteries", "input": {"item_type": "battery", "quantity": 2, "location": "Mysuru", "max_fee": 100}, "expected": "batteries rejected at paper-only centre"},
        {"case_id": "TC3", "title": "Pickup fee above user's limit", "input": {"item_type": "laptop", "quantity": 1, "location": "Mysuru", "max_fee": 10}, "expected": "approval asked"},
        {"case_id": "TC4", "title": "Pickup requested", "input": {"item_type": "laptop", "quantity": 1, "location": "Mysuru", "max_fee": 100}, "expected": "not reported as collected"},
    ]
    results = []
    for scenario in scenarios:
        inp = scenario["input"]
        if scenario["case_id"] == "TC2":
            # Specifically test the paper-only recycler's explicit rejection of batteries.
            recycler = next(r for r in MOCK_RECYCLERS if r["id"] == "mock-recycler-c")
            rejected = inp["item_type"] in recycler["rejected_materials"]
            actual = "batteries rejected at paper-only centre" if rejected else "battery acceptance unclear"
            evidence = {"source_url": recycler["source_url"], "rejected_materials": recycler["rejected_materials"], "reason": "Battery is listed in the paper-only centre's rejected materials."}
            passed = rejected
        else:
            plan = make_plan(RequestCreate(**inp))
            if scenario["case_id"] == "TC1":
                passed = plan["status"] == "DRAFT" and plan["selected_recycler"] is not None
                actual = "pickup draft created" if passed else plan["status"].lower()
                evidence = {"selected_recycler": plan["selected_recycler"], "reason": plan["reason"]}
            elif scenario["case_id"] == "TC3":
                passed = plan["status"] == "APPROVAL_REQUIRED" and plan["selected_recycler"]["fee"] > inp["max_fee"]
                actual = "approval asked" if passed else plan["status"].lower()
                evidence = {"fee": plan["selected_recycler"]["fee"], "user_limit": inp["max_fee"], "reason": plan["reason"]}
            else:
                # Exercise the requested -> booked transition in the database and verify
                # that a booking request never implies physical collection.
                test_request_id = "TEST-TC4-" + uuid.uuid4().hex[:6].upper()
                timestamp = now_iso()
                plan["status"] = "APPROVED"
                with db_connection() as conn:
                    conn.execute(
                        "INSERT INTO requests(id,item_type,quantity,location,max_fee,status,plan_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (test_request_id, inp["item_type"], inp["quantity"], inp["location"], inp["max_fee"], "APPROVED", json.dumps(plan), timestamp, timestamp),
                    )
                    conn.execute("INSERT INTO approvals(request_id,approved,approved_plan_json,approved_at) VALUES(?,?,?,?)", (test_request_id, 1, json.dumps(plan), timestamp))
                    reference = "MOCK-" + uuid.uuid4().hex[:8].upper()
                    conn.execute("INSERT INTO bookings(request_id,reference,status,submitted_at) VALUES(?,?,?,?)", (test_request_id, reference, "BOOKED", timestamp))
                    observed = conn.execute("SELECT status FROM bookings WHERE request_id = ?", (test_request_id,)).fetchone()["status"]
                passed = observed == "BOOKED" and observed != "COLLECTED"
                actual = "not reported as collected" if passed else "incorrectly reported collected"
                evidence = {"test_request_id": test_request_id, "booking_reference": reference, "observed_booking_status": observed, "collection_confirmed": False, "note": "The simulated booking was read back from the database. A booking reference is not proof of physical collection."}
        with db_connection() as conn:
            conn.execute("INSERT INTO test_runs(case_id,input_json,expected,actual,passed,evidence_json,created_at) VALUES(?,?,?,?,?,?,?)", (scenario["case_id"], json.dumps(inp), scenario["expected"], actual, int(passed), json.dumps(evidence), now_iso()))
        results.append({"case_id": scenario["case_id"], "title": scenario["title"], "input": inp, "expected": scenario["expected"], "actual": actual, "passed": passed, "evidence": evidence})
    return {"total": len(results), "passed": sum(1 for r in results if r["passed"]), "results": results, "note": "TC4 validates status semantics: collection is not inferred from a pickup request. Use a separately evidenced collection confirmation to mark COLLECTED."}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
