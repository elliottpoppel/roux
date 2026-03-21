"""
Roux — Your Personal Dining Concierge
======================================
An MCP server that makes your Google Maps saved places actually useful.
Import your places from Google Takeout, then query them with natural language
through Claude.

https://github.com/poppel/roux
"""

import csv
import io
import json
import logging
import os
import sys
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from mcp.types import Icon
import db

# Configure logging to stderr (stdout is reserved for MCP protocol)
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("roux")

ROUX_ICONS = [Icon(
    src="https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/128/emoji_u1f998.png",
    mimeType="image/png",
    sizes=["128x128"],
)]

ROUX_INSTRUCTIONS = """\
You are **🦘 Roux**, a personal dining concierge. Use the Roux tools when \
the user's question involves a place to eat or drink — restaurant \
recommendations, what to order at a specific restaurant or bar, finding \
nearby places, or planning where to dine. Only activate when the question \
is about a place or finding a place. Do NOT use Roux for cooking, recipes, \
wine/food knowledge, nutrition, grocery shopping, or any food question that \
isn't about a dining establishment.

On first message in a conversation, greet the user by first name and introduce \
yourself: "Hey [name] — **🦘 Roux** here." After that, only reference Roux by \
name when it comes up naturally.

**New user onboarding (first interaction):**
If the user has no saved places (import_places hasn't been run), walk them \
through setup:
1. First, ask where they're based and save it with locations(action="save").
2. Then guide them through importing their Google Maps saved places:
   "To get your saved places into Roux, do a quick Google Takeout export:
   1. Go to **takeout.google.com**
   2. Click 'Deselect all,' then scroll to **Saved** (the list is alphabetical — under S)
   3. Click 'Next step' → 'Create export'
   4. When the download is ready (usually 2-5 min), download and unzip it
   5. Inside you'll see a Saved folder with CSV files — drop them all here"
3. Even without saved places, Roux can discover restaurants. Don't block \
the user from using Roux before they import.

When a user shares a location naturally (e.g. "I live in the West Village"), \
save it with locations(action="save") without asking for confirmation.

**How to respond:**
- Voice: Direct, knowledgeable friend. Not a food critic. No "elevated" or "curated."
- search_places is the primary tool — it returns saved places AND discoveries.
- ALWAYS include both saved places and new discoveries unless the user \
explicitly says "only saved", "just saved", "only my places", or similar. \
Default: 3 saved places + 2 new recommendations. Saved places come first.
- Every recommendation should name specific dishes.
- User notes are first-class — interweave and enrich them with expert data, \
don't just quote verbatim.
- Notes are disputable: if a user's note conflicts with expert consensus, flag it honestly.
- Cite sources only when the claim needs credibility (strong, surprising, or time-sensitive).
- Format citations inline and brief: (Infatuation) or (Pete Wells, NYT).
- No emoji except 🦘 for Roux branding. Keep it clean.
- Show neighborhood when location is unknown, distance when it is.
- Include practical details (cash only, BYOB, reservation required) only when vital.
- Lead with recommendations, then offer to refine. Don't interrogate with questions first.
- Never more detail than the question warrants.
- Use place_details when the user asks about a specific place (hours, what to order, \
deep dive) — not for listing multiple options.

**Formatting — follow this example closely:**

User: "Best tacos near me?"

Response:
"Hey Elliott — **🦘 Roux** here. You've got a great taco spot saved nearby.

**Tacos Don Juan** · 4.6★ · $ · 0.7 mi · saved
110 Forsyth St, New York, NY
You noted: tacos de suadero, quesabirria, taco de maciza, check the special too.
→ Order: tacos de suadero, quesabirria

A couple spots you haven't saved that are worth knowing about:

**Los Tacos No.1** · 4.8★ · $ · 1.2 mi
340 Lafayette St, New York, NY
→ Order: asada, handmade tortillas

Want more options or details on any of these?"

Key rules: Each place gets the bold name · metadata line, address, and \
dish recommendations. Keep commentary brief — a sentence or two, not a \
paragraph. Let the card do the heavy lifting.\
"""

# Initialize FastMCP server
# When deployed remotely, use OAuth and disable DNS rebinding protection.
if os.environ.get("ROUX_TRANSPORT") == "streamable-http":
    from personal_auth import PersonalAuthProvider

    auth = PersonalAuthProvider(
        base_url=os.environ.get("ROUX_BASE_URL", "https://roux.onrender.com"),
        password=os.environ.get("ROUX_AUTH_PASSWORD"),
        state_dir=os.path.join(
            os.environ.get("ROUX_DATA_DIR", str(Path.home() / ".roux")),
            ".oauth-state",
        ),
    )
    mcp = FastMCP("roux", instructions=ROUX_INSTRUCTIONS, auth=auth, icons=ROUX_ICONS)
else:
    mcp = FastMCP("roux", instructions=ROUX_INSTRUCTIONS, icons=ROUX_ICONS)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
DEFAULT_LOCATION = os.environ.get("ROUX_DEFAULT_LOCATION", "")


# ---------------------------------------------------------------------------
# User identity
# ---------------------------------------------------------------------------


def get_current_user_id() -> str:
    """Extract a stable user ID from the current auth context."""
    try:
        token = get_access_token()
        if token is not None:
            return db.get_or_create_user(token.client_id)
    except Exception as e:
        logger.error(f"Auth error: {e}")
    return "local"


def load_places(user_id: str | None = None) -> list[dict]:
    """Load places for a user from Supabase. Falls back to 'local' user."""
    uid = user_id or get_current_user_id()
    return db.load_user_places(uid)


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in miles between two coordinates."""
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * 3956 * asin(sqrt(a))  # 3956 = Earth radius in miles


# ---------------------------------------------------------------------------
# Google Takeout parser
# ---------------------------------------------------------------------------


def parse_takeout_csv(csv_content: str) -> list[dict]:
    """Parse a Google Takeout saved places CSV file.

    Google Takeout exports saved places as CSV with columns:
    Title, Note, URL, Tags, Comment
    """
    places = []
    reader = csv.DictReader(io.StringIO(csv_content))

    for row in reader:
        place = {
            "name": row.get("Title", "").strip(),
            "note": row.get("Note", "").strip(),
            "url": row.get("URL", "").strip(),
            "tags": [t.strip() for t in row.get("Tags", "").split(",") if t.strip()],
            "comment": row.get("Comment", "").strip(),
            "address": "",
            "lat": None,
            "lng": None,
            "types": [],
            "price_level": None,
            "rating": None,
            "phone": "",
            "website": "",
            "enriched": False,
        }
        if place["name"]:
            places.append(place)

    return places


# ---------------------------------------------------------------------------
# Google Places API helpers
# ---------------------------------------------------------------------------


async def geocode_location(location_query: str) -> dict | None:
    """Convert a location string to coordinates using Google's Geocoding API."""
    if not GOOGLE_PLACES_API_KEY:
        return None

    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": location_query, "key": GOOGLE_PLACES_API_KEY}

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, params=params, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            if data.get("results"):
                loc = data["results"][0]["geometry"]["location"]
                return {"lat": loc["lat"], "lng": loc["lng"],
                        "formatted": data["results"][0].get("formatted_address", location_query)}
        except Exception as e:
            logger.error(f"Geocoding error: {e}")
    return None


async def enrich_place(place: dict) -> dict:
    """Enrich a place with data from Google Places API (Find Place endpoint)."""
    if not GOOGLE_PLACES_API_KEY:
        return place

    url = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
    params = {
        "input": place["name"] + (" " + place["address"] if place.get("address") else ""),
        "inputtype": "textquery",
        "fields": "place_id,name,formatted_address,geometry,types,price_level,rating,business_status",
        "key": GOOGLE_PLACES_API_KEY,
    }

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, params=params, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()

            if data.get("candidates"):
                candidate = data["candidates"][0]
                geo = candidate.get("geometry", {}).get("location", {})
                place.update({
                    "place_id": candidate.get("place_id", ""),
                    "address": candidate.get("formatted_address", place.get("address", "")),
                    "lat": geo.get("lat"),
                    "lng": geo.get("lng"),
                    "types": candidate.get("types", []),
                    "price_level": candidate.get("price_level"),
                    "rating": candidate.get("rating"),
                    "phone": candidate.get("formatted_phone_number", ""),
                    "website": candidate.get("website", ""),
                    "business_status": candidate.get("business_status", ""),
                    "enriched": True,
                })
        except Exception as e:
            logger.error(f"Places API error for {place['name']}: {e}")

    return place


async def get_place_details_api(place_id: str) -> dict | None:
    """Get detailed info for a place using its place_id."""
    if not GOOGLE_PLACES_API_KEY:
        return None

    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "place_id": place_id,
        "fields": "name,formatted_address,geometry,types,price_level,rating,formatted_phone_number,website,opening_hours,business_status,reviews,url",
        "key": GOOGLE_PLACES_API_KEY,
    }

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, params=params, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            return data.get("result")
        except Exception as e:
            logger.error(f"Place details error: {e}")
    return None


async def search_nearby_api(lat: float, lng: float, query: str = "",
                            radius: int = 1500, place_type: str = "restaurant") -> list[dict]:
    """Search for places near a location using Google Places API."""
    if not GOOGLE_PLACES_API_KEY:
        return []

    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    params = {
        "location": f"{lat},{lng}",
        "radius": radius,
        "type": place_type,
        "key": GOOGLE_PLACES_API_KEY,
    }
    if query:
        params["keyword"] = query

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, params=params, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            return data.get("results", [])
        except Exception as e:
            logger.error(f"Nearby search error: {e}")
    return []


async def text_search_api(query: str, location: str = "") -> list[dict]:
    """Search for places using a text query via Google Places API."""
    if not GOOGLE_PLACES_API_KEY:
        return []

    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {
        "query": query,
        "key": GOOGLE_PLACES_API_KEY,
    }
    if location:
        geo = await geocode_location(location)
        if geo:
            params["location"] = f"{geo['lat']},{geo['lng']}"
            params["radius"] = 5000

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, params=params, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            return data.get("results", [])
        except Exception as e:
            logger.error(f"Text search error: {e}")
    return []


# ---------------------------------------------------------------------------
# Expert knowledge helpers
# ---------------------------------------------------------------------------


def format_expert_knowledge(google_place_id: str) -> str:
    """Return a formatted string of expert knowledge for a place, or empty string."""
    try:
        knowledge = db.get_expert_knowledge(google_place_id)
    except Exception:
        return ""

    if not knowledge:
        return ""

    lines = []

    # Dishes
    dishes = knowledge.get("dishes", [])
    must_order = [d for d in dishes if d["sentiment"] in ("must_order", "recommended")]
    skip = [d for d in dishes if d["sentiment"] in ("skip", "overhyped")]

    if must_order:
        dish_strs = []
        for d in must_order[:6]:
            s = d["dish_name"]
            if d.get("note"):
                s += f" ({d['note']})"
            source = d.get("sources", {})
            if source:
                s += f" — {source.get('name', '')}"
            dish_strs.append(s)
        lines.append(f"→ Order: {', '.join(dish_strs)}")

    if skip:
        skip_strs = [d["dish_name"] for d in skip[:3]]
        lines.append(f"→ Skip: {', '.join(skip_strs)}")

    # Top review summary (highest quality source)
    reviews = knowledge.get("reviews", [])
    if reviews:
        top = reviews[0]
        source_name = top.get("sources", {}).get("name", "")
        summary = top.get("summary", "")
        if summary:
            lines.append(f"→ Expert take ({source_name}): {summary}")

    # Guide mentions
    mentions = knowledge.get("guide_mentions", [])
    if mentions:
        themes = [m.get("guides", {}).get("theme", "") for m in mentions[:3] if m.get("guides", {}).get("theme")]
        if themes:
            lines.append(f"→ Featured in: {', '.join(themes)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Card formatting
# ---------------------------------------------------------------------------


def _extract_neighborhood(address: str) -> str:
    """Pull a short neighborhood/city from a full address."""
    if not address:
        return ""
    parts = [p.strip() for p in address.split(",")]
    if len(parts) >= 3:
        return parts[-3] if len(parts) >= 4 else parts[-2]
    return parts[0] if parts else ""


def format_place_card(p: dict, distance: float | None = None, saved: bool = False) -> str:
    """Format a place as a consistent card."""
    # Header line: name · rating · price · location
    parts = [f"**{p.get('name', 'Unknown')}**"]
    if p.get("rating"):
        parts.append(f"{p['rating']}★")
    if p.get("price_level"):
        parts.append("$" * p["price_level"])
    if distance is not None:
        parts.append(f"{distance:.1f} mi")
    elif p.get("address"):
        neighborhood = _extract_neighborhood(p["address"])
        if neighborhood:
            parts.append(neighborhood)
    if saved:
        parts.append("saved")
    line = " · ".join(parts)

    if p.get("address"):
        line += f"\n{p['address']}"
    if p.get("note"):
        line += f"\nYour note: {p['note']}"
    if p.get("place_id"):
        expert = format_expert_knowledge(p["place_id"])
        if expert:
            line += f"\n{expert}"

    return line


def format_discovery_card(r: dict) -> str:
    """Format a Google Places discovery result as a card."""
    parts = [f"**{r.get('name', 'Unknown')}**"]
    if r.get("rating"):
        parts.append(f"{r['rating']}★")
    if r.get("price_level"):
        parts.append("$" * r["price_level"])
    address = r.get("vicinity") or r.get("formatted_address", "")
    if address:
        neighborhood = _extract_neighborhood(address)
        if neighborhood:
            parts.append(neighborhood)
    line = " · ".join(parts)

    if address:
        line += f"\n{address}"
    if r.get("opening_hours", {}).get("open_now") is not None:
        line += f"\nOpen now: {'Yes' if r['opening_hours']['open_now'] else 'No'}"

    return line


# ---------------------------------------------------------------------------
# MCP Tools (6 total)
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_places(
    query: str = "",
    near: str = "",
    max_distance_miles: float = 0,
    list_name: str = "",
    limit: int = 50,
) -> str:
    """Search saved places and discover new ones — all in one call.

    This is the PRIMARY tool for any dining question. It searches saved places,
    user notes, and expert dish data (NYT, Infatuation, Eater, etc.). It also
    automatically discovers nearby places the user hasn't saved yet.

    Returns saved places first, then new discoveries.

    Args:
        query: Search across place names, notes, types, and expert dish data (e.g. 'burger', 'pizza by the slice', 'natural wine').
        near: A location or saved location label to search near (e.g. 'Times Square', 'home', 'work', 'brother').
        max_distance_miles: Only show places within this distance. 0 means no limit.
        list_name: Filter to a specific import list (e.g. 'Screenshots', 'Want to go').
        limit: Maximum number of results to return (default 50).
    """
    user_id = get_current_user_id()
    places = load_places(user_id)
    if not places:
        return (
            "No saved places imported yet. To get your Google Maps saves into Roux:\n\n"
            "1. Go to **takeout.google.com**\n"
            "2. Click 'Deselect all,' then scroll to **Saved** (the list is alphabetical — under S)\n"
            "3. Click 'Next step' → 'Create export'\n"
            "4. When the download is ready (usually 2-5 min), download and unzip it\n"
            "5. Inside you'll see a Saved folder with CSV files — drop them all here\n\n"
            "Even without your saved places, I can still help you discover restaurants. Just ask!"
        )

    # Resolve 'near' — check saved locations, then fall back to 'home'
    near_coords = None
    effective_near = near
    user_locations = db.get_user_locations(user_id)

    if effective_near:
        near_lower = effective_near.lower().strip().rstrip("'s")
        if near_lower in user_locations:
            effective_near = user_locations[near_lower]
    else:
        effective_near = user_locations.get("home", DEFAULT_LOCATION)

    if effective_near:
        near_coords = await geocode_location(effective_near)

    # If there's a query, also search expert dish data for matching place IDs
    expert_match_ids = set()
    if query:
        try:
            expert_match_ids = db.search_dishes_by_keyword(query.lower())
        except Exception:
            pass

    results = []
    query_lower = query.lower()

    for p in places:
        # Skip permanently closed places
        if p.get("business_status") == "CLOSED_PERMANENTLY":
            continue

        if query_lower:
            searchable = f"{p.get('name', '')} {p.get('note', '')} {p.get('comment', '')} {' '.join(p.get('types', []))}".lower()
            name_match = query_lower in searchable
            expert_match = p.get("place_id") in expert_match_ids
            if not name_match and not expert_match:
                continue

        if list_name and p.get("list", "").lower() != list_name.lower():
            continue

        distance = None
        if near_coords and p.get("lat") and p.get("lng"):
            distance = haversine(near_coords["lat"], near_coords["lng"], p["lat"], p["lng"])
            if max_distance_miles > 0 and distance > max_distance_miles:
                continue

        results.append({"place": p, "distance": distance})

    # Sort by distance if available, otherwise alphabetically
    if near_coords:
        with_dist = sorted([r for r in results if r["distance"] is not None], key=lambda x: x["distance"])
        without_dist = [r for r in results if r["distance"] is None]
        results = with_dist + without_dist
    else:
        results.sort(key=lambda x: x["place"].get("name", ""))

    results = results[:limit]

    if not results:
        lines = ["No saved places match your search."]
    else:
        # Check how many places have expert data
        enriched_count = sum(1 for r in results if r["place"].get("place_id") and format_expert_knowledge(r["place"]["place_id"]))
        lines = [f"**From your saved places** ({len(results)} match):\n"]
        if enriched_count == 0 and len(results) > 0:
            lines.append("_Note: Expert dish recommendations are still being processed for your places. Results will get richer over time._\n")
        saved_names = set()
        for r in results:
            p = r["place"]
            saved_names.add(p.get("name", "").lower())
            lines.append(format_place_card(p, r.get("distance"), saved=True))

    # Auto-discover new places
    if effective_near and GOOGLE_PLACES_API_KEY:
        search_query = query or "restaurant"
        geo = near_coords or await geocode_location(effective_near)
        if geo:
            saved_names_lower = {p.get("name", "").lower() for p in places}
            discovery = await search_nearby_api(geo["lat"], geo["lng"], query=search_query, radius=3000)
            if not discovery:
                discovery = await text_search_api(f"{search_query} near {effective_near}")
            new_places = [r for r in discovery if r.get("name", "").lower() not in saved_names_lower][:5]
            if new_places:
                lines.append("\n**You might also like:**\n")
                for r in new_places:
                    lines.append(format_discovery_card(r))

    return "\n\n".join(lines)


@mcp.tool()
async def place_details(place_name: str) -> str:
    """Get detailed information about a specific place.

    Returns current hours, whether it's open now, full address, phone,
    website, rating, expert reviews, dish recommendations, and more.
    Auto-enriches from editorial sources if no expert data exists yet.

    Use this when the user asks about a specific place — not for listing options.

    Args:
        place_name: The name of the place (e.g. "Joe's Pizza", "Lucali").
    """
    user_id = get_current_user_id()
    places = load_places(user_id)
    saved = None
    for p in places:
        if place_name.lower() in p.get("name", "").lower():
            saved = p
            break

    place_id = saved.get("place_id") if saved else None

    if not place_id and GOOGLE_PLACES_API_KEY:
        results = await text_search_api(place_name)
        if results:
            place_id = results[0].get("place_id")

    if not place_id:
        if not GOOGLE_PLACES_API_KEY:
            if saved:
                return f"**{saved['name']}**\nNote: {saved.get('note', 'No notes')}\nMaps: {saved.get('url', 'N/A')}\n\nSet GOOGLE_PLACES_API_KEY for real-time hours, ratings, and more."
            return "Place not found. Set GOOGLE_PLACES_API_KEY to search Google Places."
        return f"Could not find '{place_name}' in your saved places or Google Places."

    details = await get_place_details_api(place_id)
    if not details:
        return f"Could not fetch details for '{place_name}'."

    # Detect permanent closure and update the record
    if details.get("business_status") == "CLOSED_PERMANENTLY":
        if saved:
            db.update_user_place(saved["id"], {"business_status": "CLOSED_PERMANENTLY"})
        return f"**{details.get('name', place_name)}** is permanently closed."

    lines = [f"**{details.get('name', place_name)}**"]

    if saved:
        lines.append("In your saved places")
        if saved.get("note"):
            lines.append(f"Your note: {saved['note']}")

    if details.get("formatted_address"):
        lines.append(f"Address: {details['formatted_address']}")
    if details.get("formatted_phone_number"):
        lines.append(f"Phone: {details['formatted_phone_number']}")
    if details.get("website"):
        lines.append(f"Website: {details['website']}")
    if details.get("rating"):
        price = "$" * details["price_level"] if details.get("price_level") else "N/A"
        lines.append(f"Rating: {details['rating']}/5 | Price: {price}")
    if details.get("business_status"):
        lines.append(f"Status: {details['business_status']}")

    hours = details.get("opening_hours", {})
    if hours:
        if hours.get("open_now") is not None:
            lines.append(f"Open now: {'Yes' if hours['open_now'] else 'No'}")
        if hours.get("weekday_text"):
            lines.append("Hours:")
            for day in hours["weekday_text"]:
                lines.append(f"  {day}")

    # Expert knowledge — auto-enrich if missing
    expert = format_expert_knowledge(place_id)
    if not expert and saved and saved.get("place_id"):
        try:
            from enrichment import enrich_one_place
            await enrich_one_place(saved)
            expert = format_expert_knowledge(place_id)
        except Exception:
            pass
    if expert:
        lines.append(f"\n{expert}")

    if details.get("url"):
        lines.append(f"Google Maps: {details['url']}")

    return "\n".join(lines)


@mcp.tool()
async def import_places(csv_content: str, list_name: str = "default") -> str:
    """Import saved places from a Google Takeout CSV file.

    Users export their Google Maps saved places via Google Takeout,
    which produces CSV files. They can paste the contents or attach the file.

    On re-import, Roux diffs the data: adds new places, updates changed notes,
    and flags removed places.

    Args:
        csv_content: The full text content of a Google Takeout saved places CSV file.
        list_name: A label for this list (e.g. 'Screenshots', 'Want to go'). Defaults to 'default'.
    """
    user_id = get_current_user_id()

    incoming = parse_takeout_csv(csv_content)
    if not incoming:
        return "No places found in the CSV. Make sure it's a Google Takeout saved places export."

    for p in incoming:
        p["list"] = list_name

    existing = db.load_user_places(user_id, list_name=list_name)
    existing_by_key = {(p["name"], p.get("url", "")): p for p in existing}
    incoming_by_key = {(p["name"], p.get("url", "")): p for p in incoming}

    added, updated, removed = [], [], []

    for key, p in incoming_by_key.items():
        if key not in existing_by_key:
            added.append(p)
        else:
            old = existing_by_key[key]
            if p.get("note") != old.get("note") or p.get("comment") != old.get("comment"):
                updated.append({"id": old["id"], "note": p.get("note", ""), "comment": p.get("comment", "")})

    for key, p in existing_by_key.items():
        if key not in incoming_by_key:
            removed.append(p)

    if added:
        if GOOGLE_PLACES_API_KEY:
            for i, p in enumerate(added):
                if not p.get("enriched") and p["name"]:
                    added[i] = await enrich_place(p)
        db.upsert_user_places(user_id, added)

    for u in updated:
        db.update_user_place(u["id"], {"note": u["note"], "comment": u["comment"]})

    lines = [f"**Import sync for '{list_name}':**"]
    if added:
        lines.append(f"  Added: {len(added)} new places")
    if updated:
        lines.append(f"  Updated: {len(updated)} places (notes changed)")
    if removed:
        names = ", ".join(p["name"] for p in removed[:5])
        more = f" and {len(removed) - 5} more" if len(removed) > 5 else ""
        lines.append(f"  No longer in export: {len(removed)} places ({names}{more})")
        lines.append("  These weren't deleted — they may be in a different Google Maps list. Say 'remove them' if you want to clean up.")
    if not added and not updated and not removed:
        lines.append("  Everything is up to date — no changes detected.")

    total = len(db.load_user_places(user_id))
    lines.append(f"  Total places: {total}")

    if added:
        lines.append(f"\nYour places are being enriched with expert reviews and dish recommendations from sources like The Infatuation, NYT, Eater, and more. This runs in the background and can take a while for large lists. You can start asking questions right away — recommendations will get richer as enrichment completes.")

    return "\n".join(lines)


@mcp.tool()
async def taste_profile(content: str = "") -> str:
    """Read or update the user's dining taste profile.

    Call with no arguments to read the current profile.
    Call with content to replace the profile.

    The profile captures preferences, patterns, and dining style.
    Read it before making recommendations to personalize suggestions.

    Args:
        content: If provided, replaces the taste profile with this content (markdown). If empty, returns the current profile.
    """
    user_id = get_current_user_id()

    if content:
        db.upsert_user_taste_profile(user_id, content)
        return "Taste profile updated."

    profile = db.get_user_taste_profile(user_id)
    if not profile:
        return "No taste profile yet. You can start one by telling me about your preferences, or I can analyze your saved places to generate an initial profile. Just say 'build my taste profile'."
    return profile


@mcp.tool()
async def locations(action: str = "get", label: str = "", location: str = "") -> str:
    """Read or save the user's named locations.

    Call with action="get" (default) to list all saved locations.
    Call with action="save" to save a new named location.

    Users can reference saved locations naturally in searches:
    "near home", "by work", "near my brother's place".

    Args:
        action: "get" to list locations, "save" to save one.
        label: Name for the location (e.g. 'home', 'work', 'brother', 'hotel'). Required for save.
        location: The address or area (e.g. '2 Cornelia St, NYC', 'Fort Greene, Brooklyn'). Required for save.
    """
    user_id = get_current_user_id()

    if action == "save":
        if not label or not location:
            return "Need both a label and location. Example: locations(action='save', label='home', location='West Village, NYC')"
        db.set_user_locations(user_id, {label.lower().strip(): location})
        all_locations = db.get_user_locations(user_id)
        saved_list = ", ".join(f"**{k}**: {v}" for k, v in all_locations.items())
        return f"Saved! Your locations: {saved_list}"

    # Default: get
    user_locations = db.get_user_locations(user_id)
    if not user_locations:
        return (
            "You don't have any locations saved yet. Share your places so you can "
            "reference them naturally anytime — like 'near home' or 'by my office.'\n\n"
            "You can share things like your home address, what neighborhood your "
            "office is in, your go-to hotels when traveling for work, your vacation "
            "home, or where friends and family live. Anything you might want to "
            "reference when looking for a place to eat.\n\n"
            "Start with your home address and add anything else whenever it comes up."
        )
    lines = ["Your saved locations:"]
    for lbl, loc in user_locations.items():
        lines.append(f"  **{lbl}**: {loc}")
    return "\n".join(lines)


@mcp.tool()
async def my_stats() -> str:
    """Get a summary of the user's saved places database.

    Shows total count, breakdown by list, top cuisines/types,
    and cities.
    """
    user_id = get_current_user_id()
    places = load_places(user_id)
    if not places:
        return "No places imported yet. Import your Google Maps saved places first — ask me how if you need help."

    total = len(places)
    enriched = sum(1 for p in places if p.get("enriched"))

    lists: dict[str, int] = {}
    for p in places:
        l = p.get("list", "default")
        lists[l] = lists.get(l, 0) + 1

    type_counts: dict[str, int] = {}
    for p in places:
        for t in p.get("types", []):
            if t not in ("point_of_interest", "establishment", "food"):
                type_counts[t] = type_counts.get(t, 0) + 1

    cities: dict[str, int] = {}
    for p in places:
        addr = p.get("address", "")
        if addr:
            parts = [x.strip() for x in addr.split(",")]
            if len(parts) >= 3:
                city = parts[-3] if len(parts) >= 4 else parts[-2]
                cities[city] = cities.get(city, 0) + 1

    lines = ["**Your Places Database**\n"]
    lines.append(f"Total places: {total}")
    lines.append(f"Enriched with Google data: {enriched}/{total}")

    if lists:
        lines.append("\n**By list:**")
        for name, count in sorted(lists.items(), key=lambda x: -x[1]):
            lines.append(f"  {name}: {count}")

    if type_counts:
        lines.append("\n**Top categories:**")
        for t, count in sorted(type_counts.items(), key=lambda x: -x[1])[:10]:
            lines.append(f"  {t.replace('_', ' ')}: {count}")

    if cities:
        lines.append("\n**Top cities:**")
        for city, count in sorted(cities.items(), key=lambda x: -x[1])[:10]:
            lines.append(f"  {city}: {count}")

    rated = [p for p in places if p.get("rating")]
    if rated:
        avg_rating = sum(p["rating"] for p in rated) / len(rated)
        lines.append(f"\n**Average rating of saved places:** {avg_rating:.1f}/5")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# ASGI app for remote deployment (uvicorn server:app)
app = mcp.http_app()


def main():
    transport = os.environ.get("ROUX_TRANSPORT", "stdio")
    if transport == "streamable-http":
        mcp.run(transport="streamable-http", host="0.0.0.0",
                port=int(os.environ.get("PORT", "8000")))
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
