from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import httpx
from bs4 import BeautifulSoup

from db import db_connection, fetchone_dict, now_iso

logger = logging.getLogger("ecoroute.mock_recyclers")

MATERIAL_LABELS: dict[str, str] = {
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

# Four distinct mock recyclers as specified in the hackathon brief
SEED_RECYCLERS: list[dict[str, Any]] = [
    {
        "id": "mock-recycler-a",
        "slug": "greenloop",
        "name": "GreenLoop Electronics Recovery",
        "area": "Mysuru",
        "pin_prefixes": ["570"],
        "accepted_materials": [
            "laptop", "mobile_phone", "desktop_computer", "tablet",
            "television", "printer", "camera", "keyboard_mouse",
            "cable_charger", "audio_device"
        ],
        "rejected_materials": ["battery", "paper", "mixed_ewaste"],
        "pickup_fee_inr": 50,
        "pickup_available": True,
        "notes": "Accredited electronics recovery center. Laptop and desktop specialist. Batteries and paper are explicitly rejected.",
    },
    {
        "id": "mock-recycler-b",
        "slug": "voltcycle",
        "aliases": ["circular-earth"],
        "name": "VoltCycle Battery & Tech Logistics",
        "area": "Mysuru",
        "pin_prefixes": ["570"],
        "accepted_materials": [
            "battery", "mobile_phone", "laptop", "cable_charger", "audio_device"
        ],
        "rejected_materials": ["paper", "television", "desktop_computer"],
        "pickup_fee_inr": 180,
        "pickup_available": True,
        "notes": "Specialized hazardous battery recovery centre. Also takes mobile tech. Paper and large TVs are rejected.",
    },
    {
        "id": "mock-recycler-c",
        "slug": "paperback",
        "name": "PaperBack Materials Centre",
        "area": "Mysuru",
        "pin_prefixes": ["570"],
        "accepted_materials": ["paper"],
        "rejected_materials": [
            "battery", "laptop", "mobile_phone", "desktop_computer",
            "television", "tablet", "keyboard_mouse", "printer",
            "cable_charger", "camera", "audio_device", "mixed_ewaste"
        ],
        "pickup_fee_inr": 20,
        "pickup_available": True,
        "notes": "Dedicated paper pulp and cardboard reclamation mill. Does not accept electronics or batteries under any circumstances.",
    },
    {
        "id": "mock-recycler-d",
        "slug": "apex",
        "name": "Apex EcoCollect Special Services",
        "area": "Mysuru",
        "pin_prefixes": ["570"],
        "accepted_materials": [
            "laptop", "mobile_phone", "desktop_computer", "battery",
            "paper", "television", "tablet", "printer", "camera",
            "keyboard_mouse", "cable_charger", "audio_device", "mixed_ewaste"
        ],
        "rejected_materials": [],
        "pickup_fee_inr": 350,
        "pickup_available": True,
        "notes": "Synthetic demo profile for special services covering mixed electronics, batteries, and paper. The INR 350 fee and availability are illustrative only; no real collection is arranged.",
    },
]


def get_public_base_url() -> str:
    """Returns a configurable public base URL for deployed source links without broken localhost links."""
    override = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if override:
        return override
    vercel_url = os.getenv("VERCEL_URL", "").strip().rstrip("/")
    if vercel_url:
        return f"https://{vercel_url.removeprefix('https://').removeprefix('http://')}"
    render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if render_url:
        return render_url
    render_hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip().rstrip("/")
    if render_hostname:
        return f"https://{render_hostname}"
    port = os.getenv("PORT", "8000")
    return f"http://127.0.0.1:{port}"


def get_source_url(slug: str) -> str:
    base = get_public_base_url()
    return f"{base}/mock-recycler/{slug}"


def render_mock_recycler_html(slug: str) -> Optional[str]:
    """Generates the semantic HTML profile for a mock recycler site."""
    slug_clean = slug.strip().lower()
    recycler = next(
        (r for r in SEED_RECYCLERS if r["slug"] == slug_clean or slug_clean in r.get("aliases", [])),
        None
    )
    if not recycler:
        return None

    accepted_lis = "\n".join(
        f'<li data-material-id="{m}">{MATERIAL_LABELS.get(m, m)}</li>'
        for m in recycler["accepted_materials"]
    )
    rejected_lis = "\n".join(
        f'<li data-material-id="{m}">{MATERIAL_LABELS.get(m, m)}</li>'
        for m in recycler["rejected_materials"]
    ) or "<li>None</li>"

    source_url = get_source_url(recycler["slug"])
    rendered_at = now_iso()

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{recycler['name']} — ReLoop Mock Recycler Profile</title>
  <meta name="robots" content="noindex, nofollow">
</head>
<body>
  <main id="recycler-profile" data-recycler-id="{recycler['id']}" data-slug="{recycler['slug']}">
    <h1 id="recycler-name">{recycler['name']}</h1>
    <p><strong>Service Area:</strong> <span id="service-area">{recycler['area']}</span></p>

    <h2>Accepted Materials</h2>
    <ul id="accepted-materials">
      {accepted_lis}
    </ul>

    <h2>Rejected Materials (Not Accepted)</h2>
    <ul id="rejected-materials">
      {rejected_lis}
    </ul>

    <p><strong>Pickup Fee:</strong> ₹<span id="pickup-fee-inr">{recycler['pickup_fee_inr']}</span> (INR)</p>
    <p><strong>Pickup Available:</strong> <span id="pickup-available">{'Yes' if recycler['pickup_available'] else 'No'}</span></p>
    <p id="recycler-notes">{recycler['notes']}</p>

    <div id="source-metadata">
      <span id="source-url">{source_url}</span>
      <span id="generated-at">{rendered_at}</span>
    </div>

    <div class="disclaimer">
      <p>Synthetic demonstration profile. Permitted mock evidence site for ReLoop coordinator. No real commercial collection is conducted or arranged.</p>
    </div>
  </main>
</body>
</html>"""


def parse_recycler_html(html_content: str, source_url: str) -> dict[str, Any]:
    """Parses the mock recycler HTML page using BeautifulSoup to extract evidence-backed data."""
    soup = BeautifulSoup(html_content, "html.parser")
    profile = soup.find(id="recycler-profile")
    if not profile:
        raise ValueError("Malformed mock recycler page: missing #recycler-profile element.")

    recycler_id = profile.get("data-recycler-id", "").strip()
    name = _required_text(soup, "recycler-name")
    area = _required_text(soup, "service-area")
    if not recycler_id:
        raise ValueError("Malformed mock recycler page: missing recycler ID.")

    accepted_ul = soup.find(id="accepted-materials")
    rejected_ul = soup.find(id="rejected-materials")
    if accepted_ul is None or rejected_ul is None:
        raise ValueError("Malformed mock recycler page: missing accepted or rejected materials.")

    accepted = _parse_materials(accepted_ul)
    rejected = _parse_materials(rejected_ul)
    fee_text = _required_text(soup, "pickup-fee-inr")
    try:
        fee = float(fee_text)
    except ValueError as exc:
        raise ValueError("Malformed mock recycler page: pickup fee is not numeric.") from exc

    availability = _required_text(soup, "pickup-available").lower()
    if availability not in ("yes", "no"):
        raise ValueError("Malformed mock recycler page: pickup availability must be Yes or No.")
    source_url_from_html = _required_text(soup, "source-url")
    retrieved_at = _required_text(soup, "generated-at")
    notes = _required_text(soup, "recycler-notes")

    return {
        "id": recycler_id,
        "name": name,
        "area": area,
        "accepted_materials": accepted,
        "rejected_materials": rejected,
        "pickup_fee": fee,
        "currency": "INR",
        "pickup_available": availability == "yes",
        "source_url": source_url_from_html,
        "notes": notes,
        "retrieved_at": retrieved_at,
        "is_mock_data": True,
        "evidence_verified": True,
    }


def _required_text(soup: BeautifulSoup, element_id: str) -> str:
    element = soup.find(id=element_id)
    value = element.get_text(strip=True) if element else ""
    if not value:
        raise ValueError(f"Malformed mock recycler page: missing #{element_id}.")
    return value


def _parse_materials(container: Any) -> list[str]:
    materials = []
    for item in container.find_all("li"):
        material_id = item.get("data-material-id")
        if material_id and material_id.strip():
            materials.append(material_id.strip())
            continue
        if item.get_text(strip=True).lower() == "none":
            continue
        raise ValueError("Malformed mock recycler page: material entry is missing its material ID.")
    return materials


async def _fetch_asgi_source(app_instance: Any, source_url: str, slug: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app_instance)
    source = httpx.URL(source_url)
    base_url = source.copy_with(path="/", query=None, fragment=None)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
        return await client.get(f"/mock-recycler/{slug}", timeout=4.0)


def retrieve_mock_recycler_evidence(slug: str, app_instance: Any = None) -> dict[str, Any]:
    """Fetches the mock source page over HTTPX and parses only the returned HTML."""
    slug_clean = slug.strip().lower()
    source_url = get_source_url(slug_clean)
    try:
        if app_instance is not None:
            response = asyncio.run(_fetch_asgi_source(app_instance, source_url, slug_clean))
        else:
            with httpx.Client(timeout=4.0) as client:
                response = client.get(source_url)
    except httpx.TimeoutException:
        return {
            "status": "unresolved",
            "error": f"Timed out fetching mock recycler source: {source_url}",
            "source_url": source_url,
            "retrieved_at": now_iso(),
        }
    except ValueError:
        return {
            "status": "unresolved",
            "error": f"Invalid mock recycler source URL: {source_url}",
            "source_url": source_url,
            "retrieved_at": now_iso(),
        }
    except (httpx.HTTPError, RuntimeError) as exc:
        return {
            "status": "unresolved",
            "error": f"Failed to fetch mock recycler source {source_url}: {exc}",
            "source_url": source_url,
            "retrieved_at": now_iso(),
        }

    if response.status_code != 200:
        return {
            "status": "unresolved",
            "error": f"Mock recycler source returned HTTP {response.status_code}: {source_url}",
            "source_url": source_url,
            "retrieved_at": now_iso(),
        }

    try:
        data = parse_recycler_html(response.text, source_url)
        # Persist extracted source evidence into mock_recycler_sources table
        try:
            with db_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO mock_recycler_sources(recycler_id, name, source_url, content_html, extracted_json, retrieved_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(recycler_id) DO UPDATE SET
                        name=EXCLUDED.name,
                        source_url=EXCLUDED.source_url,
                        content_html=EXCLUDED.content_html,
                        extracted_json=EXCLUDED.extracted_json,
                        retrieved_at=EXCLUDED.retrieved_at
                    """,
                    (data["id"], data["name"], data["source_url"], response.text, json.dumps(data), data["retrieved_at"]),
                )
        except Exception as db_err:
            logger.warning("Could not persist mock recycler source: %s", db_err)

        return data
    except Exception as parse_err:
        return {
            "status": "unresolved",
            "error": f"Failed to parse fetched mock recycler HTML from {source_url}: {parse_err}",
            "source_url": source_url,
            "retrieved_at": now_iso(),
        }


def retrieve_all_mock_recyclers(app_instance: Any = None) -> list[dict[str, Any]]:
    """Retrieves and parses evidence for all seeded mock recyclers."""
    results = []
    for seed in SEED_RECYCLERS:
        ev = retrieve_mock_recycler_evidence(seed["slug"], app_instance=app_instance)
        if "error" not in ev and ev.get("id"):
            results.append(ev)
    return results
