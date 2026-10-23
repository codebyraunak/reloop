from __future__ import annotations

import json
import copy
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import HTMLResponse

# ---------------------------------------------------------------------------
# Test Isolation Setup: Use an isolated temporary SQLite database
# Never touches Supabase PostgreSQL or production environment.
# ---------------------------------------------------------------------------
_tmp_db = tempfile.NamedTemporaryFile(prefix="ecoroute-test-isolated-", suffix=".db", delete=False)
_tmp_db.close()

os.environ["ECOROUTE_DB"] = _tmp_db.name
os.environ["TEST_MODE"] = "1"
os.environ["PUBLIC_BASE_URL"] = "http://testserver"

# Ensure ecoroute/backend is in sys.path
import sys
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from main import app, init_db, db_connection, fetchone_dict, fetchall_dict, read_mock_recycler_website  # noqa: E402
import main as main_module  # noqa: E402
import db as db_module  # noqa: E402
import agent as agent_module  # noqa: E402
from mock_recyclers import retrieve_mock_recycler_evidence, reset_demo_scenarios  # noqa: E402

init_db()
client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_demo_scenario_overrides():
    reset_demo_scenarios()
    yield
    reset_demo_scenarios()


def test_database_import_does_not_initialize_sqlite(tmp_path):
    database_path = tmp_path / "import.db"
    env = os.environ.copy()
    env["ECOROUTE_DB"] = str(database_path)
    env["TEST_MODE"] = "1"
    env["DATABASE_URL"] = "postgresql://invalid:invalid@127.0.0.1:1/unused"

    subprocess.run(
        [sys.executable, "-c", "import main"],
        cwd=BACKEND_DIR,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert not database_path.exists()


def test_explicit_sqlite_path_overrides_database_url(monkeypatch, tmp_path):
    database_path = tmp_path / "isolated.db"
    monkeypatch.setenv("ECOROUTE_DB", str(database_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:invalid@127.0.0.1:1/unused")
    monkeypatch.delenv("TEST_MODE", raising=False)

    with db_connection() as conn:
        assert conn.is_postgres is False
        conn.execute("SELECT 1")

    assert database_path.exists()


def test_configured_postgres_failure_does_not_fall_back_to_sqlite(monkeypatch, tmp_path):
    database_path = tmp_path / "must-not-be-created.db"
    monkeypatch.setenv("DATABASE_URL", "postgresql://audit.invalid/database")
    monkeypatch.setenv("ECOROUTE_DB", str(database_path))
    monkeypatch.setenv("TEST_MODE", "1")
    monkeypatch.setattr(db_module, "is_test_mode", lambda: False)
    monkeypatch.setattr(db_module, "_PG_AVAILABLE", True)

    def fail_postgres_connection(_url):
        raise OSError("simulated database outage")

    monkeypatch.setattr(db_module, "_connect_postgres", fail_postgres_connection)

    with pytest.raises(RuntimeError, match="SQLite fallback is disabled"):
        with db_module.db_connection():
            pytest.fail("a failed configured PostgreSQL connection must not use SQLite")

    assert not database_path.exists()


def test_configured_database_uses_postgres_in_non_test_mode(monkeypatch, tmp_path):
    database_path = tmp_path / "must-not-be-used.db"
    database_url = "postgresql://audit.invalid/database"

    class FakePostgresConnection:
        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("ECOROUTE_DB", str(database_path))
    monkeypatch.setenv("TEST_MODE", "1")
    monkeypatch.setattr(db_module, "is_test_mode", lambda: False)
    monkeypatch.setattr(db_module, "_PG_AVAILABLE", True)
    monkeypatch.setattr(
        db_module,
        "_connect_postgres",
        lambda configured_url: FakePostgresConnection()
        if configured_url == database_url
        else pytest.fail("configured DATABASE_URL was not forwarded"),
    )

    with db_module.db_connection() as conn:
        assert conn.is_postgres is True

    assert not database_path.exists()


def test_database_initializes_on_app_startup(monkeypatch, tmp_path):
    database_path = tmp_path / "startup.db"
    monkeypatch.setenv("ECOROUTE_DB", str(database_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:invalid@127.0.0.1:1/unused")
    monkeypatch.setenv("TEST_MODE", "1")

    with TestClient(app):
        with db_connection() as conn:
            tables = fetchall_dict(conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'requests'"
            ))

    assert database_path.exists()
    assert tables == [{"name": "requests"}]


def test_migration_normalizes_collected_status_case_insensitively(monkeypatch, tmp_path):
    database_path = tmp_path / "legacy-collected.db"
    monkeypatch.setenv("ECOROUTE_DB", str(database_path))
    monkeypatch.setenv("TEST_MODE", "1")
    init_db()

    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO requests
                (id, item_type, quantity, location, max_fee, status, plan_json, created_at, updated_at)
            VALUES ('legacy-request', 'laptop', 1, 'Mysuru', 100, 'COLLECTED', '{}', 'now', 'now')
            """
        )
        conn.execute(
            """
            INSERT INTO bookings (request_id, reference, status, submitted_at)
            VALUES ('legacy-request', 'legacy-ref', 'collected', 'now')
            """
        )

    init_db()

    with db_connection() as conn:
        request = fetchone_dict(conn.execute(
            "SELECT status FROM requests WHERE id = ?", ("legacy-request",)
        ))
        booking = fetchone_dict(conn.execute(
            "SELECT status FROM bookings WHERE request_id = ?", ("legacy-request",)
        ))

    assert request["status"] == "requested"
    assert booking["status"] == "requested"


def test_request_api_sanitizes_legacy_collected_status_in_embedded_plan():
    created = client.post("/api/requests", json={
        "item_type": "laptop",
        "quantity": 1,
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 100.0,
    })
    assert created.status_code == 200
    request_id = created.json()["id"]

    with db_connection() as conn:
        conn.execute(
            "UPDATE requests SET plan_json = ? WHERE id = ?",
            (json.dumps({"status": "COLLECTED"}), request_id),
        )
        conn.execute(
            """
            INSERT INTO test_runs(case_id, input_json, expected, actual, passed, evidence_json, explanation, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "TC4", "{}", "safe status", "legacy status", 0,
                json.dumps({"booking": {"status": "COLLECTED"}}),
                "Legacy test evidence fixture",
                "now",
            ),
        )

    response = client.get(f"/api/requests/{request_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"].casefold() != "collected"
    assert body["plan"]["status"].casefold() != "collected"

    history = client.get("/api/tests")
    assert history.status_code == 200
    legacy_run = next(row for row in history.json() if row["explanation"] == "Legacy test evidence fixture")
    assert legacy_run["evidence"]["booking"]["status"] == "requested"


# ---------------------------------------------------------------------------
# Official Scenarios TC1 – TC4
# ---------------------------------------------------------------------------

def test_tc1_laptop_draft_created():
    """TC1: Old laptop accepted by centre.
    Input: one laptop, supported location (Mysuru 570001), sufficient fee limit (₹100).
    Expected: pickup draft created with evidenced recycler acceptance.
    """
    response = client.post("/api/requests", json={
        "item_type": "laptop",
        "quantity": 1,
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 100.0,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "draft"
    assert body["plan"]["selected_recycler"] is not None
    assert body["plan"]["selected_recycler"]["recycler_id"] == "mock-recycler-a"
    assert body["plan"]["selected_recycler"]["fee"] <= 100.0
    assert "source_url" in body["plan"]["selected_recycler"]
    assert "retrieved_at" in body["plan"]["selected_recycler"]
    item = body["plan"]["items"][0]
    assert item["item_type"] == "laptop"
    assert item["quantity"] == 1
    assert item["decision"] == "accepted"
    assert item["recycler_evidence"]
    assert item["suggested_recycler"]["source_url"].endswith("/mock-recycler/greenloop")
    assert item["suggested_recycler"]["retrieved_at"]


def test_tc2_paper_accepted_batteries_rejected_by_paper_centre():
    """TC2: Centre takes paper but not batteries.
    Input: pickup request containing paper and batteries.
    Expected: paper accepted by source, batteries rejected by paper-only centre.
    """
    # 1. Verify paper-only centre HTML evidence independently
    paperback_resp = client.get("/mock-recycler/paperback")
    assert paperback_resp.status_code == 200
    assert "PaperBack Materials Centre" in paperback_resp.text
    assert "Battery" in paperback_resp.text

    # 2. Multi-item pickup request with paper and battery
    response = client.post("/api/requests", json={
        "items": [
            {"item_type": "paper", "quantity": 1},
            {"item_type": "battery", "quantity": 1},
        ],
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 250.0,
    })
    assert response.status_code == 200
    body = response.json()
    plan_items = {it["item_type"]: it for it in body["plan"]["items"]}

    assert plan_items["paper"]["decision"] == "accepted"
    assert plan_items["paper"]["quantity"] == plan_items["battery"]["quantity"] == 1
    # Battery is accepted by other local sources; PaperBack's rejection must remain
    # recycler-specific evidence rather than being misreported as a global rejection.
    assert plan_items["battery"]["decision"] == "accepted"
    paperback_paper = next(
        evidence for evidence in plan_items["paper"]["recycler_evidence"]
        if evidence["name"] == "PaperBack Materials Centre"
    )
    assert paperback_paper["decision"] == "accepted"
    assert paperback_paper["retrieved_at"]
    assert paperback_paper["source_url"].endswith("/mock-recycler/paperback")
    paperback_battery = next(
        evidence for evidence in plan_items["battery"]["recycler_evidence"]
        if evidence["name"] == "PaperBack Materials Centre"
    )
    assert paperback_battery["decision"] == "rejected"
    assert paperback_battery["retrieved_at"]
    assert paperback_battery["source_url"].endswith("/mock-recycler/paperback")
    assert body["plan"]["status"] == "draft"

    # 3. Verify that TC2 benchmark runner reflects these exact evidence-based rules
    tc2_run = client.post("/api/tests/run/TC2").json()
    assert tc2_run["passed"] is True
    assert tc2_run["input"]["items"] == [
        {"item_type": "paper", "quantity": 1},
        {"item_type": "battery", "quantity": 1},
    ]
    assert tc2_run["evidence"]["paperback_paper"]["decision"] == "accepted"
    assert tc2_run["evidence"]["paperback_battery"]["decision"] == "rejected"
    assert tc2_run["evidence"]["mixed_item_aggregate_guard"] is True


def test_rejected_item_prevents_accepted_draft_plan(monkeypatch):
    """When no source accepts an item, the real request API must not return a draft."""
    from mock_recyclers import SEED_RECYCLERS

    for recycler in SEED_RECYCLERS:
        if recycler["slug"] in ("voltcycle", "apex"):
            monkeypatch.setitem(
                recycler,
                "accepted_materials",
                [material for material in recycler["accepted_materials"] if material != "battery"],
            )

    response = client.post("/api/requests", json={
        "items": [
            {"item_type": "paper", "quantity": 1},
            {"item_type": "battery", "quantity": 1},
        ],
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 250.0,
    })
    assert response.status_code == 200
    body = response.json()
    decisions = {item["item_type"]: item["decision"] for item in body["plan"]["items"]}
    assert decisions["paper"] == "accepted"
    assert decisions["battery"] == "rejected"
    assert body["plan"]["status"] != "draft"


def test_apex_recycler_api_and_mock_source_page():
    response = client.get("/api/recyclers")
    assert response.status_code == 200

    apex = next(recycler for recycler in response.json() if recycler["id"] == "mock-recycler-d")
    assert apex["name"] == "Apex EcoCollect Special Services"
    assert apex["area"] == "Mysuru"
    assert apex["accepted_materials"] == [
        "laptop", "mobile_phone", "desktop_computer", "battery",
        "paper", "television", "tablet", "printer", "camera",
        "keyboard_mouse", "cable_charger", "audio_device", "mixed_ewaste",
    ]
    assert apex["accepted_material_labels"]
    assert apex["rejected_materials"] == []
    assert apex["pickup_fee"] == 350
    assert apex["currency"] == "INR"
    assert apex["pickup_available"] is True
    assert apex["source_url"] == "http://testserver/mock-recycler/apex"
    assert apex["is_mock_data"] is True
    assert "Synthetic demo profile" in apex["notes"]
    assert "no real collection is arranged" in apex["notes"]

    source = client.get("/mock-recycler/apex")
    assert source.status_code == 200
    assert 'data-recycler-id="mock-recycler-d"' in source.text
    assert 'data-slug="apex"' in source.text
    assert "Apex EcoCollect Special Services" in source.text
    assert 'id="accepted-materials"' in source.text
    assert 'data-material-id="laptop"' in source.text
    assert 'data-material-id="battery"' in source.text
    assert "Synthetic demonstration profile" in source.text
    assert "No real commercial collection is conducted or arranged." in source.text


def test_read_mock_recycler_website_fetches_and_parses_apex_html():
    evidence = json.loads(read_mock_recycler_website("apex"))

    assert evidence["name"] == "Apex EcoCollect Special Services"
    assert evidence["area"] == "Mysuru"
    assert "Battery" in evidence["accepted_materials"]
    assert evidence["rejected_materials"] == []
    assert evidence["pickup_fee"] == 350
    assert evidence["pickup_available"] is True
    assert evidence["source_url"] == "http://testserver/mock-recycler/apex"
    assert evidence["retrieved_at"]
    assert evidence["is_mock_data"] is True


def test_recycler_evidence_asgi_fetch_supports_explicit_local_port(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://127.0.0.1:8765")

    evidence = retrieve_mock_recycler_evidence("apex", app_instance=app)

    assert evidence["name"] == "Apex EcoCollect Special Services"
    assert evidence["source_url"] == "http://127.0.0.1:8765/mock-recycler/apex"
    assert evidence["retrieved_at"]
    assert evidence["evidence_verified"] is True
    assert "error" not in evidence


def test_invalid_recycler_source_url_returns_unresolved_evidence(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "not a valid url")

    evidence = retrieve_mock_recycler_evidence("apex", app_instance=app)

    assert evidence["status"] == "unresolved"
    assert "Invalid mock recycler source URL" in evidence["error"]
    assert "evidence_verified" not in evidence


def test_recycler_evidence_falls_back_after_http_status_failure():
    unavailable_app = FastAPI()

    @unavailable_app.get("/mock-recycler/{slug}")
    def unavailable_source(slug: str):
        return HTMLResponse("temporarily unavailable", status_code=503)

    evidence = retrieve_mock_recycler_evidence("apex", app_instance=unavailable_app)

    assert evidence["evidence_verified"] is True
    assert evidence["fetch_mode"] == "in_process_fallback"
    assert evidence["name"] == "Apex EcoCollect Special Services"
    assert evidence["source_url"] == "http://testserver/mock-recycler/apex"


def test_recycler_evidence_returns_error_when_required_html_field_is_missing():
    malformed_app = FastAPI()

    @malformed_app.get("/mock-recycler/{slug}")
    def malformed_source(slug: str):
        return HTMLResponse(
            """
            <main id="recycler-profile" data-recycler-id="mock-recycler-d">
              <h1 id="recycler-name">Apex EcoCollect Special Services</h1>
            </main>
            """
        )

    evidence = retrieve_mock_recycler_evidence("apex", app_instance=malformed_app)

    assert evidence["status"] == "unresolved"
    assert "missing #service-area" in evidence["error"]
    assert "evidence_verified" not in evidence


def test_tc3_fee_over_limit_requires_approval():
    """TC3: Pickup fee above user's limit.
    Input: Laptop request where lowest viable fee (₹50 at GreenLoop) exceeds user's limit (₹10).
    Expected: explicit exception approval requested (awaiting_approval); booking remains blocked.
    """
    response = client.post("/api/requests", json={
        "item_type": "laptop",
        "quantity": 1,
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 10.0,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "awaiting_approval"
    assert body["plan"]["requires_fee_exception"] is True

    # Booking MUST remain blocked with HTTP 409
    book_attempt = client.post(f"/api/requests/{body['id']}/book")
    assert book_attempt.status_code == 409


def test_tc4_pickup_requested_lifecycle_and_never_collected():
    """TC4: Pickup requested.
    Exercise create -> approve -> book -> verify workflow.
    Expected: reported as requested and verified; never reported as physically collected.
    No 'collected' status exists.
    """
    create_resp = client.post("/api/requests", json={
        "item_type": "laptop",
        "quantity": 1,
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 100.0,
    })
    req_id = create_resp.json()["id"]

    # 1. Draft cannot be booked directly
    assert client.post(f"/api/requests/{req_id}/book").status_code == 409

    # 2. Human approval moves status to approved
    appr_resp = client.post(f"/api/requests/{req_id}/approval", json={"approved": True})
    assert appr_resp.status_code == 200
    assert appr_resp.json()["status"] == "approved"

    # 3. Booking moves status to requested
    book_resp = client.post(f"/api/requests/{req_id}/book")
    assert book_resp.status_code == 200
    assert book_resp.json()["status"] == "requested"
    assert "reference" in book_resp.json()
    assert book_resp.json()["status"].upper() != "COLLECTED"

    # 4. Independent verification verifies booking against live mock site
    verify_resp = client.post(f"/api/requests/{req_id}/verify")
    assert verify_resp.status_code == 200
    verify_data = verify_resp.json()
    assert verify_data["verified"] is True
    assert verify_data["collection_confirmed"] is False
    assert verify_data["status"] == "verified"
    assert verify_data["display_status"] == "Requested, not yet collected."

    # 5. Database check: Verify neither lifecycle record uses a collected status.
    with db_connection() as conn:
        req_row = fetchone_dict(conn.execute("SELECT status FROM requests WHERE id = ?", (req_id,)))
        book_row = fetchone_dict(conn.execute("SELECT status FROM bookings WHERE request_id = ?", (req_id,)))
        assert req_row["status"].upper() != "COLLECTED"
        assert book_row["status"].upper() != "COLLECTED"
    request_view = client.get(f"/api/requests/{req_id}").json()
    assert request_view["status"].upper() != "COLLECTED"
    assert request_view["booking"]["status"].upper() != "COLLECTED"


# ---------------------------------------------------------------------------
# Backend Boundary Validation Tests
# ---------------------------------------------------------------------------

def test_boundary_validation_unsupported_item_type():
    with db_connection() as conn:
        before = fetchone_dict(conn.execute("SELECT COUNT(*) AS count FROM requests"))["count"]

    resp = client.post("/api/requests", json={
        "item_type": "nuclear_reactor",
        "quantity": 1,
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 100.0,
    })
    assert resp.status_code == 422
    assert "Unsupported item type" in resp.json()["detail"]
    with db_connection() as conn:
        after = fetchone_dict(conn.execute("SELECT COUNT(*) AS count FROM requests"))["count"]
    assert after == before


def test_unresolved_recycler_source_marks_affected_item_unresolved(monkeypatch):
    original_retrieve = agent_module.retrieve_mock_recycler_evidence

    def retrieve_with_missing_paperback(slug, app_instance=None, request_base_url=None):
        if slug == "paperback":
            return {
                "status": "unresolved",
                "error": "Mock recycler source returned HTTP 503.",
                "source_url": "http://testserver/mock-recycler/paperback",
                "retrieved_at": "test-retrieved-at",
            }
        return original_retrieve(
            slug,
            app_instance=app_instance,
            request_base_url=request_base_url,
        )

    monkeypatch.setattr(agent_module, "retrieve_mock_recycler_evidence", retrieve_with_missing_paperback)
    response = client.post("/api/requests", json={
        "items": [{"item_type": "paper", "quantity": 1}],
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 100,
    })

    assert response.status_code == 200
    plan = response.json()["plan"]
    assert plan["status"] == "unresolved"
    assert plan["items"][0]["decision"] == "unresolved"
    failed_source = next(
        evidence for evidence in plan["items"][0]["recycler_evidence"]
        if evidence["name"] == "PaperBack Materials Centre"
    )
    assert failed_source["decision"] == "unresolved"
    assert "HTTP 503" in failed_source["error"]
    assert response.json()["status"] != "draft"


def test_boundary_validation_invalid_quantity():
    resp = client.post("/api/requests", json={
        "item_type": "laptop",
        "quantity": 0,
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 100.0,
    })
    assert resp.status_code == 422

    resp_neg = client.post("/api/requests", json={
        "item_type": "laptop",
        "quantity": -5,
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 100.0,
    })
    assert resp_neg.status_code == 422


def test_boundary_validation_missing_city_or_invalid_pin():
    # Empty city
    resp_city = client.post("/api/requests", json={
        "item_type": "laptop",
        "quantity": 1,
        "city": "",
        "pincode": "570001",
        "max_fee": 100.0,
    })
    assert resp_city.status_code == 422

    # Invalid 5-digit PIN
    resp_pin5 = client.post("/api/requests", json={
        "item_type": "laptop",
        "quantity": 1,
        "city": "Mysuru",
        "pincode": "57000",
        "max_fee": 100.0,
    })
    assert resp_pin5.status_code == 422

    # Invalid PIN starting with 0
    resp_pin0 = client.post("/api/requests", json={
        "item_type": "laptop",
        "quantity": 1,
        "city": "Mysuru",
        "pincode": "070001",
        "max_fee": 100.0,
    })
    assert resp_pin0.status_code == 422


def test_boundary_validation_negative_fee():
    resp = client.post("/api/requests", json={
        "item_type": "laptop",
        "quantity": 1,
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": -20.0,
    })
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "updates",
    [
        {"city": ""},
        {"city": "   \t "},
        {"city": None},
        {"pincode": "57000"},
        {"pincode": "5700012"},
        {"pincode": "57A001"},
        {"pincode": "070001"},
        {"quantity": 0},
        {"quantity": -1},
        {"quantity": 1.5},
        {"quantity": None},
        {"item_type": "unlisted_material"},
        {"max_fee": -0.01},
        {"pincode": None},
    ],
)
def test_invalid_pickup_inputs_create_no_request_or_booking(updates):
    payload = {
        "item_type": "laptop",
        "quantity": 1,
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 100.0,
    }
    for field, value in updates.items():
        if value is None:
            payload.pop(field)
        else:
            payload[field] = value

    with db_connection() as conn:
        requests_before = fetchone_dict(conn.execute("SELECT COUNT(*) AS count FROM requests"))["count"]
        bookings_before = fetchone_dict(conn.execute("SELECT COUNT(*) AS count FROM bookings"))["count"]

    response = client.post("/api/requests", json=payload)

    assert response.status_code == 422
    with db_connection() as conn:
        requests_after = fetchone_dict(conn.execute("SELECT COUNT(*) AS count FROM requests"))["count"]
        bookings_after = fetchone_dict(conn.execute("SELECT COUNT(*) AS count FROM bookings"))["count"]
    assert requests_after == requests_before
    assert bookings_after == bookings_before


def test_pickup_request_accepts_zero_fee_and_stores_separate_location_fields():
    response = client.post("/api/requests", json={
        "item_type": "laptop",
        "quantity": 1,
        "city": " Mysuru ",
        "pincode": "570001",
        "max_fee": 0,
    })

    assert response.status_code == 200
    request = response.json()
    assert request["city"] == "Mysuru"
    assert request["pincode"] == "570001"
    assert request["location"] == "Mysuru 570001"
    assert request["max_fee"] == 0


# ---------------------------------------------------------------------------
# Multi-Item Requests & Independent Decisions
# ---------------------------------------------------------------------------

def test_multi_item_independent_evaluations():
    resp = client.post("/api/requests", json={
        "items": [
            {"item_type": "laptop", "quantity": 1},
            {"item_type": "paper", "quantity": 3},
            {"item_type": "battery", "quantity": 2},
        ],
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 500.0,
    })
    assert resp.status_code == 200
    plan = resp.json()["plan"]
    items = {i["item_type"]: i for i in plan["items"]}
    assert len(items) == 3
    assert items["laptop"]["decision"] == "accepted"
    assert items["paper"]["decision"] == "accepted"
    assert items["battery"]["decision"] == "accepted"
    assert items["laptop"]["suggested_recycler"]["recycler_id"] == "mock-recycler-a"


# ---------------------------------------------------------------------------
# Safeguards: Idempotency, Snapshots, and State Protection
# ---------------------------------------------------------------------------

def test_booking_idempotency():
    resp = client.post("/api/requests", json={
        "item_type": "laptop",
        "quantity": 1,
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 100.0,
    })
    req_id = resp.json()["id"]
    client.post(f"/api/requests/{req_id}/approval", json={"approved": True})

    first_book = client.post(f"/api/requests/{req_id}/book").json()
    second_book = client.post(f"/api/requests/{req_id}/book").json()

    assert first_book["reference"] == second_book["reference"]
    assert "Existing booking returned" in second_book["message"]


def test_cannot_modify_approval_after_booking():
    resp = client.post("/api/requests", json={
        "item_type": "laptop",
        "quantity": 1,
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 100.0,
    })
    req_id = resp.json()["id"]
    client.post(f"/api/requests/{req_id}/approval", json={"approved": True})
    client.post(f"/api/requests/{req_id}/book")

    # Attempt to change approval after booking must fail with HTTP 409
    change_attempt = client.post(f"/api/requests/{req_id}/approval", json={"approved": False})
    assert change_attempt.status_code == 409
    assert "already exists" in change_attempt.json()["detail"]


def test_fee_exception_approval_gating():
    resp = client.post("/api/requests", json={
        "item_type": "laptop",
        "quantity": 1,
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 20.0,
    })
    req_id = resp.json()["id"]

    # Approving without explicit fee exception acceptance must fail with HTTP 409
    fail_appr = client.post(f"/api/requests/{req_id}/approval", json={"approved": True, "accept_fee_exception": False})
    assert fail_appr.status_code == 409

    # Approving with explicit accept_fee_exception=True succeeds
    ok_appr = client.post(f"/api/requests/{req_id}/approval", json={"approved": True, "accept_fee_exception": True})
    assert ok_appr.status_code == 200
    assert ok_appr.json()["status"] == "approved"


def test_immutable_snapshot_preserved():
    resp = client.post("/api/requests", json={
        "item_type": "laptop",
        "quantity": 1,
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 100.0,
    })
    req_id = resp.json()["id"]
    client.post(f"/api/requests/{req_id}/approval", json={"approved": True})
    client.post(f"/api/requests/{req_id}/book")

    with db_connection() as conn:
        appr_row = fetchone_dict(conn.execute("SELECT * FROM approvals WHERE request_id = ?", (req_id,)))
        book_row = fetchone_dict(conn.execute("SELECT * FROM bookings WHERE request_id = ?", (req_id,)))

    assert appr_row["approved_plan_json"] is not None
    assert book_row["approved_snapshot_json"] is not None
    assert appr_row["approved_plan_json"] == book_row["approved_snapshot_json"]


# ---------------------------------------------------------------------------
# Bounded Agent Trace & Assistant Accuracy
# ---------------------------------------------------------------------------

def test_agent_trace_shape_and_persistence():
    resp = client.post("/api/requests", json={
        "item_type": "laptop",
        "quantity": 1,
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 100.0,
    })
    req_id = resp.json()["id"]
    traces = resp.json()["agent_traces"]

    assert len(traces) == 6
    assert resp.json()["agent_mode"] == "deterministic"
    assert resp.json()["plan"]["agent_mode"] == "deterministic"
    expected_tools = [
        "gather_and_validate_details",
        "retrieve_mock_recycler_profiles",
        "compare_acceptance_rules",
        "compare_pickup_fees",
        "prepare_pickup_plan_with_evidence",
        "verify_evidence_completeness",
    ]
    for idx, expected_tool in enumerate(expected_tools):
        t = traces[idx]
        assert t["step"] == idx + 1
        assert t["tool"] == expected_tool
        assert t["actor"] == "backend"
        assert "timestamp" in t
        assert "evidence" in t


def test_gemini_orchestration_uses_fake_client_tools_and_retries_once(monkeypatch):
    tool_names = [
        "validate_details",
        "fetch_recycler_profiles",
        "match_items",
        "compare_fees",
        "draft_plan",
    ]

    def response_with_calls(names):
        parts = [
            SimpleNamespace(function_call=SimpleNamespace(name=name, args={}))
            for name in names
        ]
        return SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))],
            text=None,
        )

    class RetryableError(Exception):
        status_code = 503

    class FakeModels:
        def __init__(self):
            self.calls = []
            self.responses = [
                RetryableError("temporary server failure"),
                response_with_calls(tool_names),
                SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[]))],
                                text="Fetched HTML lists 'Laptop' in accepted materials. "
                                     "Accepted by GreenLoop Electronics Recovery (listed pickup fee ₹50.0)."),
            ]

        def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

    fake = SimpleNamespace(models=FakeModels())
    monkeypatch.setattr(agent_module, "TEST_MODE", False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")

    coordinator = agent_module.BoundedAgentCoordinator(
        request_id="EW-FAKEGEM",
        app_instance=app,
        request_base_url="http://testserver",
    )
    monkeypatch.setattr(coordinator, "_create_gemini_client", lambda: fake)
    result = coordinator.run_agent_workflow(
        items=[{"item_type": "laptop", "quantity": 1}],
        city="Mysuru",
        pincode="570001",
        max_fee=100,
    )

    assert result["agent_mode"] == "gemini"
    assert result["plan"]["agent_mode"] == "gemini"
    assert result["explanation"] == (
        "Fetched HTML lists 'Laptop' in accepted materials. "
        "Accepted by GreenLoop Electronics Recovery (listed pickup fee ₹50.0)."
    )
    assert len(fake.models.calls) == 3
    assert [trace["actor"] for trace in result["agent_trace"]] == [
        "gemini", "gemini", "gemini", "gemini", "gemini", "backend"
    ]
    assert all("api_key" not in str(call).lower() for call in fake.models.calls)
    declarations = fake.models.calls[1]["config"]["tools"][0]["function_declarations"]
    assert [declaration["name"] for declaration in declarations] == tool_names
    assert all(declaration["parameters"]["properties"] == {} for declaration in declarations)
    function_responses = fake.models.calls[2]["contents"][-1]["parts"]
    assert len(function_responses) == len(tool_names)
    assert all("function_response" in part for part in function_responses)


def test_gemini_tool_order_runs_prerequisites_on_backend(monkeypatch):
    fake_response = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            function_call=SimpleNamespace(name="draft_plan", args={})
                        )
                    ]
                )
            )
        ],
        text=None,
    )
    final_response = SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[]))],
        text="The result is safe and accepted.",
    )

    class FakeModels:
        def __init__(self):
            self.responses = [fake_response, final_response]

        def generate_content(self, **kwargs):
            return self.responses.pop(0)

    monkeypatch.setattr(agent_module, "TEST_MODE", False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    coordinator = agent_module.BoundedAgentCoordinator(
        request_id="EW-FAKEORDER",
        app_instance=app,
        request_base_url="http://testserver",
    )
    monkeypatch.setattr(
        coordinator,
        "_create_gemini_client",
        lambda: SimpleNamespace(models=FakeModels()),
    )
    result = coordinator.run_agent_workflow(
        items=[{"item_type": "laptop", "quantity": 1}],
        city="Mysuru",
        pincode="570001",
        max_fee=100,
    )

    assert result["agent_mode"] == "gemini_partial"
    assert [trace["actor"] for trace in result["agent_trace"]] == [
        "backend", "backend", "backend", "backend", "gemini", "backend"
    ]
    assert result.get("explanation") is None


def test_gemini_tool_execution_cap_returns_responses_for_all_calls(monkeypatch):
    repeated_calls = [
        SimpleNamespace(function_call=SimpleNamespace(name="validate_details", args={}))
        for _ in range(8)
    ]
    tool_response = SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=repeated_calls))],
        text=None,
    )
    final_response = SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[]))],
        text="No validated tool quote is returned.",
    )

    class FakeModels:
        def __init__(self):
            self.responses = [tool_response, final_response]
            self.contents = []

        def generate_content(self, **kwargs):
            self.contents.append(copy.deepcopy(kwargs["contents"]))
            return self.responses.pop(0)

    monkeypatch.setattr(agent_module, "TEST_MODE", False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    fake = SimpleNamespace(models=FakeModels())
    coordinator = agent_module.BoundedAgentCoordinator(
        request_id="EW-FAKECAP",
        app_instance=app,
        request_base_url="http://testserver",
    )
    monkeypatch.setattr(coordinator, "_create_gemini_client", lambda: fake)
    result = coordinator.run_agent_workflow(
        items=[{"item_type": "laptop", "quantity": 1}],
        city="Mysuru",
        pincode="570001",
        max_fee=100,
    )

    responses = fake.models.contents[1][-1]["parts"]
    assert len(responses) == 8
    assert all("function_response" in part for part in responses)
    assert sum(
        "Tool execution limit reached" in part["function_response"]["response"].get("error", "")
        for part in responses
    ) == 2
    assert result["agent_mode"] == "gemini_partial"
    assert sum(trace["actor"] == "gemini" for trace in result["agent_trace"]) == 1


def test_test_mode_never_creates_a_gemini_client(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "configured-but-unused-in-tests")
    coordinator = agent_module.BoundedAgentCoordinator(
        request_id="EW-TESTMODE",
        app_instance=app,
        request_base_url="http://testserver",
    )

    def unexpected_client():
        raise AssertionError("TEST_MODE must not initialize Gemini.")

    monkeypatch.setattr(coordinator, "_create_gemini_client", unexpected_client)
    result = coordinator.run_agent_workflow(
        items=[{"item_type": "laptop", "quantity": 1}],
        city="Mysuru",
        pincode="570001",
        max_fee=100,
    )

    assert result["agent_mode"] == "deterministic"
    assert result["agent_exception"] is None
    assert all(trace["actor"] == "backend" for trace in result["agent_trace"])


def test_gemini_failure_falls_back_with_exception_type_only(monkeypatch):
    class BadRequest(Exception):
        status_code = 400

    class FakeModels:
        def generate_content(self, **kwargs):
            raise BadRequest("secret fake key must not be returned")

    monkeypatch.setattr(agent_module, "TEST_MODE", False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    coordinator = agent_module.BoundedAgentCoordinator(
        request_id="EW-FAKEFAIL",
        app_instance=app,
        request_base_url="http://testserver",
    )
    monkeypatch.setattr(
        coordinator,
        "_create_gemini_client",
        lambda: SimpleNamespace(models=FakeModels()),
    )
    result = coordinator.run_agent_workflow(
        items=[{"item_type": "laptop", "quantity": 1}],
        city="Mysuru",
        pincode="570001",
        max_fee=100,
    )

    assert result["agent_mode"] == "deterministic"
    assert result["agent_exception"] == "BadRequest"
    assert "secret fake key" not in json.dumps(result)
    assert all(trace["actor"] == "backend" for trace in result["agent_trace"])


def test_assistant_separates_budget_from_quantity():
    # "I have one old laptop in Mysuru and my budget is ₹100"
    # MUST NOT extract quantity as 100!
    resp = client.post("/api/assistant", json={
        "message": "I have one old laptop in Mysuru and my budget is ₹100"
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["item_type"] == "laptop"
    assert body["quantity"] == 1
    assert body["max_fee"] == 100.0
    assert body["booking_performed"] is False
    assert body["ai_powered"] is False
    assert body["fallback_used"] is True
    assert body["tool_called"] is None


def test_assistant_never_submits_a_booking():
    resp = client.post("/api/assistant", json={
        "message": "Please book a pickup for my 2 laptops right now"
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["booking_performed"] is False


# ---------------------------------------------------------------------------
# Dedicated /tests Page & Health
# ---------------------------------------------------------------------------

def test_tests_page_served():
    resp = client.get("/tests")
    assert resp.status_code == 200
    assert '<link rel="icon" type="image/png" href="/favicon/reloop-logo.png" />' in resp.text
    assert '<img class="brand-mark" src="/favicon/reloop-logo.png" alt="" />' in resp.text
    assert "Official Benchmark Tests" in resp.text
    assert "Run All Scenarios" in resp.text
    assert "card-TC1" in resp.text
    assert "card-TC2" in resp.text
    assert "card-TC3" in resp.text
    assert "card-TC4" in resp.text
    assert "PaperBack's fetched HTML accepts paper and rejects batteries" in resp.text
    assert "another recycler may accept batteries" in resp.text
    assert "postBenchmark('/api/tests/run')" in resp.text
    assert "fetch(path" in resp.text
    assert "encodeURIComponent(caseId)" in resp.text
    assert "fetch('/api/tests')" in resp.text
    assert "API_BASE" not in resp.text


def test_homepage_references_reloop_favicon_and_brand_logo():
    response = client.get("/")
    assert response.status_code == 200
    assert '<link rel="icon" type="image/png" href="/favicon/reloop-logo.png" />' in response.text
    assert response.text.count('<img class="brand-mark" src="/favicon/reloop-logo.png" alt="" />') == 2


def test_favicon_route_serves_allowlisted_png():
    response = client.get("/favicon/reloop-logo.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert response.content == (BACKEND_DIR / "static" / "images" / "reloop-logo.png").read_bytes()

    unsupported = client.get("/favicon/other.png")
    assert unsupported.status_code == 404


def test_tests_page_run_all_script_targets_real_benchmark_api():
    page = client.get("/tests")
    assert page.status_code == 200
    assert "postBenchmark('/api/tests/run')" in page.text
    assert "TC5 · BONUS" in page.text

    response = client.post("/api/tests/run")
    assert response.status_code == 200
    result = response.json()
    assert result["total"] == 5
    assert result["official_total"] == 4
    assert result["bonus_total"] == 1
    assert len(result["results"]) == 5
    assert all(case["passed"] is True for case in result["results"])


def test_official_tests_api_run_and_persisted():
    resp = client.post("/api/tests/run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert body["official_total"] == 4
    assert body["bonus_total"] == 1
    assert body["passed"] == 5
    for result in body["results"]:
        assert result["input"]
        assert result["expected"]
        assert result["actual"]
        assert result["passed"] is True
        assert result["evidence"]

    tc3 = next(result for result in body["results"] if result["case_id"] == "TC3")
    assert tc3["evidence"]["booking_before_approval"]["status_code"] == 409
    assert tc3["evidence"]["approval_without_fee_exception"]["status_code"] == 409
    assert tc3["evidence"]["approval_with_fee_exception"]["response_status"] == "approved"
    assert tc3["evidence"]["booking_after_approval"]["status"] == "requested"

    tc4 = next(result for result in body["results"] if result["case_id"] == "TC4")
    assert tc4["evidence"]["verified"] is True
    assert tc4["evidence"]["collection_confirmed"] is False

    tc5 = next(result for result in body["results"] if result["case_id"] == "TC5")
    assert tc5["passed"] is True
    assert tc5["evidence"]["replacement_has_no_approval_or_booking"] is True
    assert tc4["evidence"]["display_wording"] == "Requested, not yet collected."
    assert tc4["evidence"]["no_collected_status"] is True

    # Verify runs were persisted in test_runs table
    history = client.get("/api/tests").json()
    assert len(history) >= 4
    assert any(h["case_id"] == "TC1" for h in history)
    assert any(h["case_id"] == "TC4" for h in history)


def test_individual_benchmark_cases_and_repeated_runs_are_independent():
    results = {}
    for case_id in ("TC1", "TC2", "TC3", "TC4"):
        response = client.post(f"/api/tests/run/{case_id}")
        assert response.status_code == 200
        result = response.json()
        assert result["case_id"] == case_id
        assert result["passed"] is True
        assert result["input"]
        assert result["expected"]
        assert result["actual"]
        assert result["evidence"]
        results[case_id] = result

    repeated_tc4 = client.post("/api/tests/run/TC4")
    assert repeated_tc4.status_code == 200
    repeated_result = repeated_tc4.json()
    assert repeated_result["passed"] is True
    assert repeated_result["evidence"]["request_id"] != results["TC4"]["evidence"]["request_id"]
    assert repeated_result["evidence"]["booking_reference"] != results["TC4"]["evidence"]["booking_reference"]


def test_health_endpoint():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "EcoRoute"
    assert body["database_status"] == "connected"
    assert body["database_dialect"] == "sqlite"
    assert isinstance(body["gemini_configured"], bool)
    assert body["agent_orchestration_enabled"] is False
    assert "DATABASE_URL" not in body


def test_no_collected_status_exists_anywhere():
    """Confirms collection marking stays disabled and no stored lifecycle state is collected."""
    resp = client.post("/api/requests/TEST-123/collection", json={"collection_confirmed": True})
    assert resp.status_code in (404, 405)

    with db_connection() as conn:
        request_rows = fetchall_dict(conn.execute(
            "SELECT status FROM requests WHERE LOWER(status) = 'collected'"
        ))
        booking_rows = fetchall_dict(conn.execute(
            "SELECT status FROM bookings WHERE LOWER(status) = 'collected'"
        ))
    assert request_rows == []
    assert booking_rows == []


def test_multi_centre_booking_snapshot_verifies_each_assignment():
    created = client.post("/api/requests", json={
        "items": [
            {"item_type": "paper", "quantity": 1},
            {"item_type": "battery", "quantity": 1},
        ],
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 250,
    })
    assert created.status_code == 200
    request = created.json()
    assert request["status"] == "draft"
    plan = request["plan"]
    assert {row["recycler_id"] for row in plan["centres"]} == {
        "mock-recycler-b", "mock-recycler-c",
    }

    approval = client.post(
        f"/api/requests/{request['id']}/approval",
        json={"approved": True},
    )
    assert approval.status_code == 200
    booking = client.post(f"/api/requests/{request['id']}/book")
    assert booking.status_code == 200

    snapshot = client.get(f"/api/requests/{request['id']}").json()["booking"]["approved_snapshot"]
    assert {row["recycler_id"] for row in snapshot["centres"]} == {
        "mock-recycler-b", "mock-recycler-c",
    }
    verification = client.post(f"/api/requests/{request['id']}/verify")
    assert verification.status_code == 200
    result = verification.json()
    assert result["status"] == "verified"
    assert result["verified"] is True
    assert any(row["check"].endswith("accepts:paper") and row["ok"] for row in result["checks"])
    assert any(row["check"].endswith("accepts:battery") and row["ok"] for row in result["checks"])


def test_approval_is_immutable_after_first_decision():
    created = client.post("/api/requests", json={
        "item_type": "laptop", "quantity": 1, "city": "Mysuru",
        "pincode": "570001", "max_fee": 100,
    }).json()
    first = client.post(
        f"/api/requests/{created['id']}/approval",
        json={"approved": True},
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/requests/{created['id']}/approval",
        json={"approved": False},
    )
    assert second.status_code == 409


def test_request_collection_requires_specific_id():
    assert client.get("/api/requests").status_code == 400
    created = client.post("/api/requests", json={
        "item_type": "laptop", "quantity": 1, "city": "Mysuru",
        "pincode": "570001", "max_fee": 100,
    }).json()
    response = client.get("/api/requests", params={"request_id": created["id"]})
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [created["id"]]


@pytest.mark.parametrize(
    "scenario",
    ["fee_up", "stops_accepting_laptop", "pickup_unavailable"],
)
def test_live_recycler_drift_fails_verification_and_replans_without_booking(scenario):
    created = client.post("/api/requests", json={
        "item_type": "laptop", "quantity": 1, "city": "Mysuru",
        "pincode": "570001", "max_fee": 100,
    }).json()
    request_id = created["id"]
    approved = client.post(
        f"/api/requests/{request_id}/approval", json={"approved": True}
    )
    assert approved.status_code == 200
    booking = client.post(f"/api/requests/{request_id}/book")
    assert booking.status_code == 200
    changed = client.post(
        "/api/demo/scenario",
        json={"slug": "greenloop", "scenario": scenario},
    )
    assert changed.status_code == 200

    verification = client.post(f"/api/requests/{request_id}/verify").json()
    assert verification["status"] == "failed"
    assert verification["verified"] is False
    assert verification["drift"]
    assert all({"centre", "field", "approved_value", "live_value"} <= set(row) for row in verification["drift"])

    replacement_response = client.post(f"/api/requests/{request_id}/replan")
    assert replacement_response.status_code == 200
    replacement = replacement_response.json()
    assert replacement["replaces_request_id"] == request_id
    assert replacement["status"] in {"draft", "awaiting_approval"}
    assert replacement["approval"] is None
    assert replacement["booking"] is None
    assert all(
        centre["recycler_id"] != "mock-recycler-a"
        for centre in replacement["plan"]["centres"]
    )


def test_unreachable_live_recycler_is_unresolved_not_failed(monkeypatch):
    created = client.post("/api/requests", json={
        "item_type": "laptop", "quantity": 1, "city": "Mysuru",
        "pincode": "570001", "max_fee": 100,
    }).json()
    request_id = created["id"]
    assert client.post(
        f"/api/requests/{request_id}/approval", json={"approved": True}
    ).status_code == 200
    assert client.post(f"/api/requests/{request_id}/book").status_code == 200

    original_retrieve = main_module.retrieve_mock_recycler_evidence

    def unavailable(slug, app_instance=None, request_base_url=None):
        if slug == "greenloop":
            return {
                "status": "unresolved",
                "error": "simulated source outage",
                "source_url": "https://example.invalid/mock-recycler/greenloop",
                "retrieved_at": "2026-10-23T00:00:00+00:00",
            }
        return original_retrieve(
            slug,
            app_instance=app_instance,
            request_base_url=request_base_url,
        )

    monkeypatch.setattr(main_module, "retrieve_mock_recycler_evidence", unavailable)
    verification = client.post(f"/api/requests/{request_id}/verify").json()
    assert verification["status"] == "unresolved"
    assert verification["verified"] is False
    assert verification["verified_at"] is None
    stored = client.get(f"/api/requests/{request_id}").json()
    assert stored["booking"]["verification_outcome"] == "unresolved"
    assert stored["booking"]["verified_at"] is None


def test_tc5_bonus_benchmark_runs_self_healing_flow():
    response = client.post("/api/tests/run/TC5")
    assert response.status_code == 200
    result = response.json()
    assert result["passed"] is True
    assert result["evidence"]["verification"]["status"] == "failed"
    assert result["evidence"]["replacement_has_no_approval_or_booking"] is True


def test_pin_prefix_can_match_a_centre_when_city_text_does_not():
    response = client.post("/api/requests", json={
        "item_type": "laptop",
        "quantity": 1,
        "city": "Bengaluru",
        "pincode": "570001",
        "max_fee": 100,
    })
    assert response.status_code == 200
    evidence = response.json()["plan"]["items"][0]["recycler_evidence"]
    greenloop = next(row for row in evidence if row["recycler_id"] == "mock-recycler-a")
    assert greenloop["area_match"] == "pin_prefix"
    assert greenloop["matched_pin_prefix"] == "570"


def test_partial_plan_needs_explicit_acknowledgement():
    created = client.post("/api/requests", json={
        "items": [
            {"item_type": "laptop", "quantity": 1},
            {"item_type": "battery", "quantity": 1},
        ],
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 100,
        "preferred_centre": "mock-recycler-a",
    })
    assert created.status_code == 200
    request = created.json()
    assert request["status"] == "awaiting_approval"
    assert [item["decision"] for item in request["plan"]["items"]] == ["accepted", "rejected"]
    assert [centre["recycler_id"] for centre in request["plan"]["centres"]] == ["mock-recycler-a"]
    assert {
        row["recycler_id"]
        for item in request["plan"]["items"]
        for row in item["recycler_evidence"]
    } == {"mock-recycler-a"}

    rejected = client.post(
        f"/api/requests/{request['id']}/approval",
        json={"approved": True},
    )
    assert rejected.status_code == 409
    assert "acknowledgement" in rejected.json()["detail"].lower()
    approved = client.post(
        f"/api/requests/{request['id']}/approval",
        json={"approved": True, "acknowledge_partial": True},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"


def test_duplicate_basket_items_are_merged_at_the_api_boundary():
    response = client.post("/api/requests", json={
        "items": [
            {"item_type": "laptop", "quantity": 1},
            {"item_type": "laptop", "quantity": 2},
        ],
        "city": "Mysuru",
        "pincode": "570001",
        "max_fee": 100,
    })
    assert response.status_code == 200
    assert response.json()["items"] == [{"item_type": "laptop", "quantity": 3}]
    assert response.json()["plan"]["items"][0]["quantity"] == 3
