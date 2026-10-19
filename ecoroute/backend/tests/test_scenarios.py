import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Use a fresh test database for every test module run.
_tmp_db = tempfile.NamedTemporaryFile(prefix="ecoroute-test-", suffix=".db", delete=False)
_tmp_db.close()
os.environ["ECOROUTE_DB"] = _tmp_db.name

from main import app, init_db  # noqa: E402

init_db()
client = TestClient(app)


def test_tc1_laptop_draft_created():
    response = client.post("/api/requests", json={"item_type": "laptop", "quantity": 1, "location": "Mysuru", "max_fee": 100})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "DRAFT"
    assert body["plan"]["selected_recycler"]["recycler_id"] == "mock-recycler-a"


def test_tc2_battery_rejected_by_paper_only_centre():
    recyclers = {r["id"]: r for r in client.get("/api/recyclers").json()}
    paperback = recyclers["mock-recycler-c"]
    assert "battery" not in paperback["accepted_materials"]
    assert "battery" in paperback["rejected_materials"]
    runs = client.post("/api/tests/run").json()
    tc2 = next(r for r in runs["results"] if r["case_id"] == "TC2")
    assert tc2["passed"] is True


def test_tc3_fee_over_limit_requires_approval():
    response = client.post("/api/requests", json={"item_type": "laptop", "quantity": 1, "location": "Mysuru", "max_fee": 10})
    assert response.status_code == 200
    assert response.json()["status"] == "APPROVAL_REQUIRED"


def test_booking_requires_human_approval_and_never_implies_collection():
    created = client.post("/api/requests", json={"item_type": "laptop", "quantity": 1, "location": "Mysuru", "max_fee": 100}).json()
    request_id = created["id"]
    assert client.post(f"/api/requests/{request_id}/book").status_code == 409
    approved = client.post(f"/api/requests/{request_id}/approval", json={"approved": True}).json()
    assert approved["status"] == "APPROVED"
    booked = client.post(f"/api/requests/{request_id}/book").json()
    assert booked["status"] == "BOOKED"
    assert "COLLECTED" != booked["status"]
    verified = client.post(f"/api/requests/{request_id}/verify").json()
    assert verified["collection_confirmed"] is False
    assert verified["status"] == "BOOKED"


def test_duplicate_booking_is_idempotent():
    created = client.post("/api/requests", json={"item_type": "laptop", "quantity": 1, "location": "Mysuru", "max_fee": 100}).json()
    request_id = created["id"]
    client.post(f"/api/requests/{request_id}/approval", json={"approved": True})
    first = client.post(f"/api/requests/{request_id}/book").json()
    second = client.post(f"/api/requests/{request_id}/book").json()
    assert first["reference"] == second["reference"]


def test_collection_requires_explicit_evidence():
    created = client.post("/api/requests", json={"item_type": "laptop", "quantity": 1, "location": "Mysuru", "max_fee": 100}).json()
    request_id = created["id"]
    client.post(f"/api/requests/{request_id}/approval", json={"approved": True})
    client.post(f"/api/requests/{request_id}/book")
    assert client.post(f"/api/requests/{request_id}/collection", json={"collection_confirmed": True, "evidence_note": ""}).status_code == 422
    result = client.post(f"/api/requests/{request_id}/collection", json={"collection_confirmed": True, "evidence_note": "Demo operator confirmed collection"})
    assert result.status_code == 200
    assert result.json()["status"] == "COLLECTED"


def test_reloop_frontend_is_served_from_root():
    response = client.get("/")
    assert response.status_code == 200
    assert "ReLoop — E-waste Pickup & Recycling" in response.text
    assert 'id="runTestsButton"' in response.text
    assert 'id="maxFee"' in response.text


def test_assistant_never_submits_a_booking():
    response = client.post("/api/assistant", json={"message": "I have 3 mobile phones"})
    assert response.status_code == 200
    body = response.json()
    assert body["item_type"] == "mobile_phone"
    assert body["quantity"] == 3
    assert body["booking_performed"] is False


def test_extra_frontend_item_type_has_evidenced_match():
    response = client.post("/api/requests", json={"item_type": "television", "quantity": 1, "location": "Mysuru", "max_fee": 100})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "DRAFT"
    assert body["plan"]["selected_recycler"]["recycler_id"] == "mock-recycler-a"


def test_health_reports_gemini_status():
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "gemini_configured" in body
    assert body["gemini_configured"] is True


def test_assistant_gemini_powered_with_safety():
    response = client.post("/api/assistant", json={"message": "Can you recycle my 2 laptops?"})
    assert response.status_code == 200
    body = response.json()
    assert body["item_type"] == "laptop"
    assert body["quantity"] == 2
    assert body["booking_performed"] is False
    assert body["ai_powered"] is True
    assert len(body["reply"]) > 0


def test_gemini_backend_tools_deterministic_execution():
    from main import (
        collect_and_validate_pickup_details,
        search_mock_recyclers,
        read_mock_recycler_website,
        compare_fees_with_budget,
        prepare_pickup_plan_with_evidence,
    )
    import json

    # 1. Collect & validate details
    val = json.loads(collect_and_validate_pickup_details("laptop", 2, "Mysuru", 100))
    assert val["status"] == "COMPLETE"
    assert val["item_type"] == "laptop"
    assert val["quantity"] == 2

    # Incomplete details
    val_inc = json.loads(collect_and_validate_pickup_details("", 1, "", 0))
    assert val_inc["status"] == "INCOMPLETE"
    assert len(val_inc["missing_details"]) >= 2

    # 2. Search mock recyclers
    search_res = json.loads(search_mock_recyclers("laptop", "Mysuru"))
    assert len(search_res["matches"]) >= 1
    assert any(m["name"] == "GreenLoop Electronics Recovery" for m in search_res["matches"])

    # 3. Read mock recycler website
    web_res = json.loads(read_mock_recycler_website("greenloop"))
    assert web_res["name"] == "GreenLoop Electronics Recovery"
    assert "Laptop" in web_res["accepted_materials"]
    assert web_res["pickup_fee"] == 50

    # 4. Compare fees with budget
    comp_ok = json.loads(compare_fees_with_budget("laptop", "Mysuru", 100))
    assert comp_ok["fee_within_budget"] is True
    assert comp_ok["approval_required"] is False

    comp_over = json.loads(compare_fees_with_budget("laptop", "Mysuru", 20))
    assert comp_over["fee_within_budget"] is False
    assert comp_over["approval_required"] is True

    # 5. Prepare pickup plan with evidence
    plan_res = json.loads(prepare_pickup_plan_with_evidence("laptop", 1, "Mysuru", 100))
    assert plan_res["status"] == "DRAFT"
    assert plan_res["requires_human_approval"] is True
    assert "Mock source" in plan_res["evidence"]


def test_assistant_tool_calling_invocation():
    response = client.post("/api/assistant", json={"message": "I want to recycle 1 laptop in Mysuru for max fee 100"})
    assert response.status_code == 200
    body = response.json()
    assert body["booking_performed"] is False
    assert body["ai_powered"] is True
    assert body["fallback_used"] is False
    assert body["tool_called"] is not None
    assert body["tool_result"] is not None


def test_assistant_fallback_when_api_key_missing():
    import main
    orig_key = main.GEMINI_API_KEY
    try:
        main.GEMINI_API_KEY = ""
        response = client.post("/api/assistant", json={"message": "I have 1 laptop in Mysuru"})
        assert response.status_code == 200
        body = response.json()
        assert body["ai_powered"] is False
        assert body["fallback_used"] is True
        assert "[Fallback:" in body["reply"]
        assert body["error"] is not None
        assert body["booking_performed"] is False
    finally:
        main.GEMINI_API_KEY = orig_key


def test_full_request_to_booking_workflow_verification():
    # 1. Draft request
    create_res = client.post("/api/requests", json={"item_type": "laptop", "quantity": 1, "location": "Mysuru", "max_fee": 100})
    assert create_res.status_code == 200
    req_data = create_res.json()
    req_id = req_data["id"]
    assert req_data["status"] == "DRAFT"

    # 2. Cannot book before human approval
    book_fail = client.post(f"/api/requests/{req_id}/book")
    assert book_fail.status_code == 409

    # 3. Human approval
    appr_res = client.post(f"/api/requests/{req_id}/approval", json={"approved": True})
    assert appr_res.status_code == 200
    assert appr_res.json()["status"] == "APPROVED"

    # 4. Book request
    book_res = client.post(f"/api/requests/{req_id}/book")
    assert book_res.status_code == 200
    assert book_res.json()["status"] == "BOOKED"
    booking_ref = book_res.json()["reference"]

    # 5. Verify booking independently
    verify_res = client.post(f"/api/requests/{req_id}/verify")
    assert verify_res.status_code == 200
    assert verify_res.json()["verified"] is True
    assert verify_res.json()["collection_confirmed"] is False

    # 6. Collection confirmation requires evidence
    col_fail = client.post(f"/api/requests/{req_id}/collection", json={"collection_confirmed": True, "evidence_note": ""})
    assert col_fail.status_code == 422

    col_res = client.post(f"/api/requests/{req_id}/collection", json={"collection_confirmed": True, "evidence_note": "Driver handed receipt to householder"})
    assert col_res.status_code == 200
    assert col_res.json()["status"] == "COLLECTED"
    assert col_res.json()["collection_confirmed"] is True
