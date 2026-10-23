from __future__ import annotations

import json
import logging
import os
import re
import uuid
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import db_connection, now_iso
from mock_recyclers import (
    MATERIAL_LABELS,
    SEED_RECYCLERS,
    retrieve_mock_recycler_evidence,
)

logger = logging.getLogger("ecoroute.agent")

WORD_TO_NUM: dict[str, int] = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10
}

MATERIAL_ALIASES: dict[str, str] = {
    "laptop": "laptop", "laptops": "laptop", "notebook": "laptop", "notebooks": "laptop",
    "phone": "mobile_phone", "phones": "mobile_phone", "mobile": "mobile_phone", "mobiles": "mobile_phone",
    "smartphone": "mobile_phone", "smartphones": "mobile_phone",
    "computer": "desktop_computer", "computers": "desktop_computer", "desktop": "desktop_computer", "desktops": "desktop_computer", "pc": "desktop_computer",
    "battery": "battery", "batteries": "battery",
    "paper": "paper", "papers": "paper", "cardboard": "paper",
    "tv": "television", "tvs": "television", "television": "television", "televisions": "television",
    "tablet": "tablet", "tablets": "tablet", "ipad": "tablet", "ipads": "tablet",
    "keyboard": "keyboard_mouse", "keyboards": "keyboard_mouse", "mouse": "keyboard_mouse", "mice": "keyboard_mouse",
    "printer": "printer", "printers": "printer",
    "cable": "cable_charger", "cables": "cable_charger", "charger": "cable_charger", "chargers": "cable_charger",
    "camera": "camera", "cameras": "camera",
    "audio": "audio_device", "headphone": "audio_device", "headphones": "audio_device", "speaker": "audio_device", "speakers": "audio_device",
    "mixed": "mixed_ewaste", "mixed_ewaste": "mixed_ewaste",
}


def validate_pin_code(pincode: str) -> bool:
    """Validates a six-digit Indian PIN code."""
    if not pincode:
        return False
    clean = pincode.strip()
    return bool(re.match(r"^[1-9][0-9]{5}$", clean))


def extract_assistant_intent(message: str) -> dict[str, Any]:
    """Carefully extracts materials, quantities, location, and fee limit from a user message.
    CRITICAL: Never confuses the fee budget (e.g. ₹100) with item quantity!
    """
    raw = message.strip()
    lower = raw.lower()

    # 1. Separate budget / fee limit first so it is never confused with item quantity
    max_fee: Optional[float] = None
    fee_patterns = [
        r"(?:budget|fee|limit|cost|price|max|maximum)\s*(?:is|of|:)?\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
        r"(?:₹|rs\.?|inr)\s*(\d+(?:\.\d+)?)",
    ]
    budget_span = None
    for pattern in fee_patterns:
        m = re.search(pattern, lower)
        if m:
            try:
                max_fee = float(m.group(1))
                budget_span = m.span()
                break
            except Exception:
                pass

    # Remove the budget phrase from the string used to extract items and quantities
    text_for_items = lower
    if budget_span:
        text_for_items = lower[:budget_span[0]] + " " + lower[budget_span[1]:]

    # 2. Extract PIN code and City
    pin_match = re.search(r"\b([1-9][0-9]{5})\b", text_for_items)
    pincode = pin_match.group(1) if pin_match else None

    # Common cities or words around location
    city = None
    for known_city in ["mysuru", "mysore", "bengaluru", "bangalore", "mumbai", "delhi", "pune", "chennai", "hyderabad"]:
        if known_city in text_for_items:
            city = known_city.capitalize()
            if city == "Mysore":
                city = "Mysuru"
            break

    # 3. Extract items and their specific quantities
    extracted_items = []
    # Pattern: optional quantity word/number + optional adjective + item alias
    # e.g. "one old laptop", "3 mobile phones", "batteries"
    for word, item_type in sorted(MATERIAL_ALIASES.items(), key=lambda x: -len(x[0])):
        # Match whole word
        pattern = rf"\b(?:(\d+|one|two|three|four|five|six|seven|eight|nine|ten|a|an)\s+)?(?:old\s+|used\s+|broken\s+)?{re.escape(word)}\b"
        match = re.search(pattern, text_for_items)
        if match:
            qty_raw = match.group(1)
            qty = 1
            if qty_raw:
                qty = WORD_TO_NUM.get(qty_raw, 1)
                try:
                    qty = int(qty_raw)
                except ValueError:
                    pass
            qty = max(1, min(500, qty))
            # Don't add duplicate item_types
            if not any(i["item_type"] == item_type for i in extracted_items):
                extracted_items.append({"item_type": item_type, "quantity": qty, "matched_phrase": match.group(0)})

    primary_item = extracted_items[0]["item_type"] if extracted_items else None
    primary_quantity = extracted_items[0]["quantity"] if extracted_items else 1

    return {
        "items": extracted_items,
        "primary_item": primary_item,
        "quantity": primary_quantity,
        "city": city,
        "pincode": pincode,
        "max_fee": max_fee,
    }


class BoundedAgentCoordinator:
    """Bounded multi-step agent tool loop capped at ~6 steps with traceable execution."""

    def __init__(
        self,
        request_id: Optional[str] = None,
        app_instance: Any = None,
        excluded_centres: Optional[set[str]] = None,
        request_base_url: Optional[str] = None,
    ):
        self.request_id = request_id or ("EW-" + uuid.uuid4().hex[:8].upper())
        self.app_instance = app_instance
        self.excluded_centres = excluded_centres or set()
        self.request_base_url = request_base_url
        self.trace: list[dict[str, Any]] = []

    def _record_step(self, step: int, tool: str, inp: Any, output: Any, evidence: Optional[str] = None, error: Optional[str] = None) -> dict[str, Any]:
        entry = {
            "step": step,
            "tool": tool,
            "input": inp,
            "output": output,
            "evidence": evidence,
            "error": error,
            "timestamp": now_iso(),
        }
        self.trace.append(entry)
        try:
            with db_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO agent_traces(id, request_id, step, tool, input_json, output_json, evidence_json, error_json, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"{self.request_id}-step-{step}",
                        self.request_id,
                        step,
                        tool,
                        json.dumps(inp),
                        json.dumps(output),
                        json.dumps(evidence) if evidence else None,
                        json.dumps(error) if error else None,
                        entry["timestamp"],
                    ),
                )
        except Exception as exc:
            logger.warning("Could not persist agent trace: %s", exc)
        return entry

    def run_agent_workflow(
        self,
        items: list[dict[str, Any]],
        city: str,
        pincode: str,
        max_fee: float,
        preferred_centre: Optional[str] = None,
    ) -> dict[str, Any]:
        """Executes the full 6-step bounded agent workflow with traceable evidence."""
        # -------------------------------------------------------------
        # STEP 1: Gather and Validate Pickup Details
        # -------------------------------------------------------------
        val_errors = []
        clean_city = (city or "").strip()
        clean_pin = (pincode or "").strip()

        if not clean_city or len(clean_city) < 2:
            val_errors.append("City is required and must be at least 2 characters.")
        if not validate_pin_code(clean_pin):
            val_errors.append("Valid 6-digit Indian PIN code is required (e.g. 570001).")
        if max_fee < 0:
            val_errors.append("Maximum pickup fee limit cannot be negative.")
        if not items:
            val_errors.append("At least one pickup item must be specified.")
        for idx, item in enumerate(items):
            m_type = item.get("item_type")
            qty = item.get("quantity", 0)
            if m_type not in MATERIAL_LABELS:
                val_errors.append(f"Item #{idx+1} '{m_type}' is not supported. Supported: {', '.join(MATERIAL_LABELS.keys())}.")
            if not isinstance(qty, int) or qty < 1:
                val_errors.append(f"Item #{idx+1} quantity must be an integer >= 1.")

        step1_out = {
            "valid": len(val_errors) == 0,
            "errors": val_errors,
            "items_count": len(items),
            "location": f"{clean_city} {clean_pin}".strip(),
            "max_fee": max_fee,
        }
        self._record_step(
            step=1,
            tool="gather_and_validate_details",
            inp={"items": items, "city": clean_city, "pincode": clean_pin, "max_fee": max_fee},
            output=step1_out,
            evidence=f"Validated {len(items)} item(s) for {clean_city} ({clean_pin}) with fee limit ₹{max_fee}.",
            error=val_errors[0] if val_errors else None,
        )

        if not step1_out["valid"]:
            return {
                "request_id": self.request_id,
                "status": "failed",
                "reason": "Validation failed: " + "; ".join(val_errors),
                "plan": None,
                "agent_trace": self.trace,
            }

        # -------------------------------------------------------------
        # STEP 2: Retrieve Permitted Mock Recycler Profiles
        # -------------------------------------------------------------
        source_results = [
            (
                seed,
                retrieve_mock_recycler_evidence(
                    seed["slug"],
                    app_instance=self.app_instance,
                    request_base_url=self.request_base_url,
                ),
            )
            for seed in SEED_RECYCLERS
        ]
        retrieved_recyclers = [
            result for _, result in source_results
            if result.get("evidence_verified")
        ]
        unresolved_sources = [
            {
                **result,
                "recycler_id": result.get("recycler_id", seed["id"]),
                "name": result.get("name", seed["name"]),
                "area": seed["area"],
                "slug": seed["slug"],
            }
            for seed, result in source_results
            if result.get("status") == "unresolved"
        ]
        step2_out = {
            "profiles_retrieved": len(retrieved_recyclers),
            "profiles_unresolved": len(unresolved_sources),
            "recyclers": [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "area": r["area"],
                    "fee": r["pickup_fee"],
                    "source_url": r["source_url"],
                    "retrieved_at": r["retrieved_at"],
                    "fetch_mode": r.get("fetch_mode", "http"),
                }
                for r in retrieved_recyclers
            ],
            "errors": unresolved_sources,
        }
        self._record_step(
            step=2,
            tool="retrieve_mock_recycler_profiles",
            inp={"target_area": clean_city},
            output=step2_out,
            evidence=(
                f"Fetched and parsed HTML for {len(retrieved_recyclers)} mock recycler centres; "
                f"{len(unresolved_sources)} source(s) unresolved."
            ),
            error="; ".join(source.get("error", "Source unresolved") for source in unresolved_sources) or None,
        )

        # -------------------------------------------------------------
        # STEP 3: Compare Item Acceptance and Rejection Rules
        # -------------------------------------------------------------
        per_item_evaluations = []
        for item in items:
            m_type = item["item_type"]
            qty = item["quantity"]
            accepted_by = []
            rejected_by = []
            unresolved_for_item = []
            item_evidence = []

            for r in retrieved_recyclers:
                city_match = clean_city.lower() in r["area"].lower() or r["area"].lower() in clean_city.lower()
                pin_match = any(
                    clean_pin.startswith(prefix)
                    for prefix in next(
                        (seed.get("pin_prefixes", []) for seed in SEED_RECYCLERS if seed["id"] == r["id"]),
                        [],
                    )
                )
                in_area = city_match or pin_match
                if not in_area:
                    continue

                excluded = r["id"] in self.excluded_centres or r["name"] in self.excluded_centres
                preferred_match = not preferred_centre or preferred_centre in {
                    r["id"], r["name"], next(
                        (seed["slug"] for seed in SEED_RECYCLERS if seed["id"] == r["id"]),
                        "",
                    ),
                }
                if not preferred_match:
                    continue

                if m_type in r["accepted_materials"]:
                    evidence = {
                        "recycler_id": r["id"],
                        "name": r["name"],
                        "fee": r["pickup_fee"],
                        "pickup_available": r["pickup_available"],
                        "source_url": r["source_url"],
                        "retrieved_at": r["retrieved_at"],
                        "fetch_mode": r.get("fetch_mode", "http"),
                        "area_match": "city" if city_match else "pin_prefix",
                        "matched_pin_prefix": next(
                            (prefix for prefix in next(
                                (seed.get("pin_prefixes", []) for seed in SEED_RECYCLERS if seed["id"] == r["id"]),
                                [],
                            ) if clean_pin.startswith(prefix)),
                            None,
                        ),
                        "decision": "accepted",
                        "evidence": f"Fetched HTML lists '{MATERIAL_LABELS[m_type]}' in accepted materials.",
                    }
                    item_evidence.append(evidence)
                    if r["pickup_available"] and not excluded and preferred_match:
                        accepted_by.append(evidence)
                elif m_type in r["rejected_materials"]:
                    evidence = {
                        "recycler_id": r["id"],
                        "name": r["name"],
                        "decision": "rejected",
                        "source_url": r["source_url"],
                        "retrieved_at": r["retrieved_at"],
                        "fetch_mode": r.get("fetch_mode", "http"),
                        "area_match": "city" if city_match else "pin_prefix",
                        "evidence": f"Fetched HTML explicitly lists '{MATERIAL_LABELS[m_type]}' as rejected.",
                    }
                    rejected_by.append(evidence)
                    item_evidence.append(evidence)
                else:
                    item_evidence.append({
                        "recycler_id": r["id"],
                        "name": r["name"],
                        "decision": "not_listed",
                        "source_url": r["source_url"],
                        "retrieved_at": r["retrieved_at"],
                        "evidence": f"Fetched HTML does not list '{MATERIAL_LABELS[m_type]}' as accepted or rejected.",
                    })

            for source in unresolved_sources:
                if preferred_centre and preferred_centre not in {
                    source.get("recycler_id"),
                    source.get("name"),
                    source.get("slug"),
                }:
                    continue
                source_area = source["area"]
                if source_area and not (
                    clean_city.lower() in source_area.lower()
                    or source_area.lower() in clean_city.lower()
                ):
                    continue
                unresolved_for_item.append(source)
                item_evidence.append({
                    "recycler_id": source.get("recycler_id"),
                    "name": source.get("name"),
                    "decision": "unresolved",
                    "source_url": source.get("source_url"),
                    "retrieved_at": source.get("retrieved_at"),
                    "error": source.get("error"),
                })

            per_item_evaluations.append({
                "item_type": m_type,
                "item_label": MATERIAL_LABELS[m_type],
                "quantity": qty,
                "accepted_centres": accepted_by,
                "rejected_centres": rejected_by,
                "unresolved_sources": unresolved_for_item,
                "evidence": item_evidence,
                "is_accepted": bool(accepted_by) and not unresolved_for_item,
                "decision": (
                    "unresolved" if unresolved_for_item
                    else "accepted" if accepted_by
                    else "rejected" if rejected_by
                    else "unresolved"
                ),
            })

        step3_out = {"evaluations": per_item_evaluations}
        self._record_step(
            step=3,
            tool="compare_acceptance_rules",
            inp={"items": [i["item_type"] for i in items]},
            output=step3_out,
            evidence=f"Evaluated material rules across mock centres. Accepted items: {[e['item_type'] for e in per_item_evaluations if e['is_accepted']]}.",
        )

        # -------------------------------------------------------------
        # STEP 4: Compare Pickup Fees with User's Limit
        # -------------------------------------------------------------
        for evaluation in per_item_evaluations:
            evaluation["accepted_centres"].sort(key=lambda centre: centre["fee"])
            evaluation["selected_recycler"] = (
                evaluation["accepted_centres"][0]
                if evaluation["accepted_centres"] and not evaluation["unresolved_sources"]
                else None
            )
        selected_by_id = {
            evaluation["selected_recycler"]["recycler_id"]: evaluation["selected_recycler"]
            for evaluation in per_item_evaluations
            if evaluation["selected_recycler"]
        }
        selected_recycler = next(iter(selected_by_id.values())) if (
            len(selected_by_id) == 1
            and all(evaluation["selected_recycler"] for evaluation in per_item_evaluations)
        ) else None
        best_fee = sum(centre["fee"] for centre in selected_by_id.values())
        fee_exception_required = best_fee > max_fee
        centres_by_id: dict[str, dict[str, Any]] = {}
        for evaluation in per_item_evaluations:
            assignment = evaluation["selected_recycler"]
            if not assignment:
                continue
            centre = centres_by_id.setdefault(assignment["recycler_id"], {
                "recycler_id": assignment["recycler_id"],
                "recycler_name": assignment["name"],
                "fee": assignment["fee"],
                "source_url": assignment["source_url"],
                "retrieved_at": assignment["retrieved_at"],
                "items": [],
            })
            centre["items"].append({
                "item_type": evaluation["item_type"],
                "quantity": evaluation["quantity"],
            })

        step4_out = {
            "selected_centre": selected_recycler["name"] if selected_recycler else "Split / No single centre",
            "fee": best_fee,
            "max_fee_limit": max_fee,
            "within_limit": not fee_exception_required,
            "fee_exception_required": fee_exception_required,
        }
        self._record_step(
            step=4,
            tool="compare_pickup_fees",
            inp={"budget_limit": max_fee, "proposed_fee": best_fee},
            output=step4_out,
            evidence=f"Proposed fee ₹{best_fee} vs budget ₹{max_fee}. Fee exception required: {fee_exception_required}.",
        )

        # -------------------------------------------------------------
        # STEP 5: Prepare Pickup Plan with Traceable Evidence
        # -------------------------------------------------------------
        per_item_decisions = []
        for evaluation in per_item_evaluations:
            best_match = evaluation["selected_recycler"]
            decision = evaluation["decision"]
            per_item_decisions.append({
                "item_type": evaluation["item_type"],
                "item_label": evaluation["item_label"],
                "quantity": evaluation["quantity"],
                "decision": decision,
                "reason": (
                    f"Accepted by {best_match['name']} (listed pickup fee ₹{best_match['fee']})."
                    if decision == "accepted" and best_match
                    else "Recycler evidence could not be fully retrieved for this item."
                    if decision == "unresolved"
                    else "No available recycler accepts this item in the service area."
                ),
                "suggested_recycler": {
                    "recycler_id": best_match["recycler_id"],
                    "recycler_name": best_match["name"],
                    "fee": best_match["fee"],
                    "source_url": best_match["source_url"],
                    "retrieved_at": best_match["retrieved_at"],
                } if best_match and decision == "accepted" else None,
                "source_evidence": evaluation["evidence"],
                "source_url": best_match["source_url"] if best_match else (
                    evaluation["evidence"][0]["source_url"] if evaluation["evidence"] else None
                ),
                "retrieved_at": best_match["retrieved_at"] if best_match else None,
                "recycler_evidence": evaluation["evidence"],
            })

        all_accepted = all(d["decision"] == "accepted" for d in per_item_decisions)
        any_accepted = any(d["decision"] == "accepted" for d in per_item_decisions)
        any_unresolved = any(d["decision"] == "unresolved" for d in per_item_decisions)

        if any_unresolved:
            plan_status = "unresolved"
            plan_reason = "Recycler evidence could not be fetched or parsed for every requested item."
        elif not any_accepted:
            plan_status = "unresolved"
            plan_reason = "No permitted mock recycler in the service area accepted the requested materials."
        elif fee_exception_required:
            plan_status = "awaiting_approval"
            plan_reason = f"Proposed pickup fee of ₹{best_fee:.2f} exceeds your ₹{max_fee:.2f} limit. Human approval of the fee exception is required before booking."
        elif not all_accepted:
            plan_status = "awaiting_approval"
            plan_reason = "Some items were accepted while others were rejected. Review per-item decisions and approve the partial plan before booking."
        else:
            plan_status = "draft"
            centre_names = ", ".join(centre["name"] for centre in selected_by_id.values())
            plan_reason = f"Eligible recycler evidence found at {centre_names}. Fee of ₹{best_fee:.2f} is within limit ₹{max_fee:.2f}. Human approval required before booking."

        plan = {
            "request_id": self.request_id,
            "status": plan_status,
            "reason": plan_reason,
            "items": per_item_decisions,
            "total_items_count": sum(i["quantity"] for i in items),
            "city": clean_city,
            "pincode": clean_pin,
            "location": f"{clean_city} {clean_pin}".strip(),
            "max_fee": max_fee,
            "total_fee": best_fee,
            "preferred_centre": preferred_centre,
            "centres": list(centres_by_id.values()),
            "selected_recycler": {
                "recycler_id": selected_recycler["recycler_id"],
                "recycler_name": selected_recycler["name"],
                "fee": selected_recycler["fee"],
                "source_url": selected_recycler["source_url"],
                "retrieved_at": selected_recycler["retrieved_at"],
                "notes": next(
                    recycler["notes"]
                    for recycler in retrieved_recyclers
                    if recycler["id"] == selected_recycler["recycler_id"]
                ),
            } if selected_recycler else None,
            "requires_fee_exception": fee_exception_required,
            "requires_human_approval": True,
            "sources_retrieved_at": now_iso(),
            "is_mock_data": True,
        }

        self._record_step(
            step=5,
            tool="prepare_pickup_plan_with_evidence",
            inp={"all_accepted": all_accepted, "status": plan_status},
            output={"status": plan_status, "total_fee": best_fee},
            evidence=f"Plan synthesized with status '{plan_status}'. Requires human approval: True.",
        )

        # -------------------------------------------------------------
        # STEP 6: Verify Evidence Completeness
        # -------------------------------------------------------------
        completeness_checks = [
            bool(plan.get("sources_retrieved_at")),
            all(bool(d.get("recycler_evidence")) for d in per_item_decisions),
            not any_unresolved,
        ]
        evidence_complete = all(completeness_checks)

        if not evidence_complete:
            plan["status"] = "unresolved"
            plan["reason"] = "Evidence retrieval was incomplete or inconsistent across mock source profiles."

        self._record_step(
            step=6,
            tool="verify_evidence_completeness",
            inp={"checks": completeness_checks},
            output={"complete": evidence_complete, "final_status": plan["status"]},
            evidence=f"Evidence verification completed with outcome: {'complete' if evidence_complete else 'unresolved'}.",
        )

        return {
            "request_id": self.request_id,
            "status": plan["status"],
            "reason": plan["reason"],
            "plan": plan,
            "agent_trace": self.trace,
        }
