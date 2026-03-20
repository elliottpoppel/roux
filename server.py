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
    mcp = FastMCP("roux", auth=auth, icons=ROUX_ICONS)
else:
    mcp = FastMCP("roux", icons=ROUX_ICONS)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("ROUX_DATA_DIR", Path.home() / ".roux"))
PLACES_DB = DATA_DIR / "places.json"
TASTE_PROFILE = DATA_DIR / "taste-profile.md"
GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
DEFAULT_LOCATION = os.environ.get("ROUX_DEFAULT_LOCATION", "")

# On Render, seed data can be injected via secret files at /etc/secrets/.
# Copy them into DATA_DIR on first run if the database doesn't exist yet.
SECRETS_DIR = Path("/etc/secrets")
if SECRETS_DIR.exists() and not PLACES_DB.exists():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for fname in ("places.json", "taste-profile.md"):
        src = SECRETS_DIR / fname
        if src.exists():
            (DATA_DIR / fname).write_bytes(src.read_bytes())
            logger.info(f"Loaded seed data from {src}")

# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------


def ensure_data_dir():
    """Create data directory if it doesn't exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_places() -> list[dict]:
    """Load places from the local JSON database."""
    if not PLACES_DB.exists():
        return []
    with open(PLACES_DB) as f:
        return json.load(f)


def save_places(places: list[dict]):
    """Save places to the local JSON database."""
    ensure_data_dir()
    with open(PLACES_DB, "w") as f:
        json.dump(places, f, indent=2)


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
            # These will be enriched later via Google Places API
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
        lines.append(f"  Order: {', '.join(dish_strs)}")

    if skip:
        skip_strs = [d["dish_name"] for d in skip[:3]]
        lines.append(f"  Skip: {', '.join(skip_strs)}")

    # Top review summary (highest quality source)
    reviews = knowledge.get("reviews", [])
    if reviews:
        top = reviews[0]
        source_name = top.get("sources", {}).get("name", "")
        summary = top.get("summary", "")
        if summary:
            lines.append(f"  Expert take ({source_name}): {summary}")

    # Guide mentions
    mentions = knowledge.get("guide_mentions", [])
    if mentions:
        themes = [m.get("guides", {}).get("theme", "") for m in mentions[:3] if m.get("guides", {}).get("theme")]
        if themes:
            lines.append(f"  Featured in: {', '.join(themes)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def import_places(csv_content: str, list_name: str = "default") -> str:
    """Import saved places from a Google Takeout CSV file.

    Users export their Google Maps saved places via Google Takeout,
    which produces CSV files. Paste the contents of a CSV file here.

    Args:
        csv_content: The full text content of a Google Takeout saved places CSV file.
        list_name: A label for this list (e.g. 'Screenshots', 'Want to go'). Defaults to 'default'.
    """
    new_places = parse_takeout_csv(csv_content)
    if not new_places:
        return "No places found in the CSV. Make sure it's a Google Takeout saved places export."

    # Tag each place with the list name
    for p in new_places:
        p["list"] = list_name

    # Load existing and merge (avoid duplicates by name+url)
    existing = load_places()
    existing_keys = {(p["name"], p.get("url", "")) for p in existing}

    added = 0
    for p in new_places:
        if (p["name"], p.get("url", "")) not in existing_keys:
            existing.append(p)
            existing_keys.add((p["name"], p.get("url", "")))
            added += 1

    save_places(existing)

    # Try to enrich new places with Google Places API
    enriched_count = 0
    if GOOGLE_PLACES_API_KEY:
        all_places = load_places()
        for i, p in enumerate(all_places):
            if not p.get("enriched") and p["name"]:
                all_places[i] = await enrich_place(p)
                if all_places[i].get("enriched"):
                    enriched_count += 1
        save_places(all_places)

    result = f"Imported {added} new places from '{list_name}' list."
    if enriched_count:
        result += f" Enriched {enriched_count} places with Google Places data (addresses, coordinates, ratings)."
    if not GOOGLE_PLACES_API_KEY:
        result += " Note: Set GOOGLE_PLACES_API_KEY to enable automatic enrichment with addresses, hours, and ratings."

    total = len(load_places())
    result += f" Total places in database: {total}."
    return result


@mcp.tool()
async def search_my_places(
    query: str = "",
    near: str = "",
    max_distance_miles: float = 0,
    list_name: str = "",
    limit: int = 50,
) -> str:
    """Search through the user's saved places AND expert knowledge database.

    ALWAYS call this tool FIRST before discover_places. This searches the user's
    saved places, their notes, AND the expert database (editorial reviews, dish
    recommendations from NYT, Infatuation, Eater, etc.).

    The query parameter searches across: place names, user notes, Google place
    types, AND expert dish data. So searching 'burger' WILL find a steakhouse
    that serves great burgers if editorial sources mention it.

    Args:
        query: Search across place names, notes, types, and expert dish data (e.g. 'burger', 'pizza by the slice', 'natural wine').
        near: A location to search near (e.g. 'Times Square', 'Shibuya', '10001').
        max_distance_miles: Only show places within this distance. 0 means no limit.
        list_name: Filter to a specific import list (e.g. 'Screenshots', 'Want to go').
        limit: Maximum number of results to return (default 50).
    """
    places = load_places()
    if not places:
        return "No places in your database yet. Use import_places to load your Google Takeout export."

    # Resolve 'near' to coordinates
    near_coords = None
    if near:
        near_coords = await geocode_location(near)
        if not near_coords and DEFAULT_LOCATION:
            near_coords = await geocode_location(DEFAULT_LOCATION)

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
        # Text search across name and notes
        if query_lower:
            searchable = f"{p.get('name', '')} {p.get('note', '')} {p.get('comment', '')} {' '.join(p.get('types', []))}".lower()
            name_match = query_lower in searchable
            expert_match = p.get("place_id") in expert_match_ids
            if not name_match and not expert_match:
                continue

        # List filter
        if list_name and p.get("list", "").lower() != list_name.lower():
            continue

        # Distance filter
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
        filters = []
        if query:
            filters.append(f"query='{query}'")
        if near:
            filters.append(f"near='{near}'")
        if list_name:
            filters.append(f"list='{list_name}'")
        return f"No saved places match your search ({', '.join(filters)}). Try broadening your criteria, or use discover_places to search beyond your saved list."

    # Format output
    lines = [f"Found {len(results)} saved place(s):\n"]
    for r in results:
        p = r["place"]
        line = f"**{p['name']}**"
        if p.get("address"):
            line += f"\n  Address: {p['address']}"
        if p.get("rating"):
            price = "$" * p["price_level"] if p.get("price_level") else ""
            line += f"\n  Rating: {p['rating']}/5" + (f" | Price: {price}" if price else "")
        if p.get("note"):
            line += f"\n  Your note: {p['note']}"
        if p.get("comment"):
            line += f"\n  Comment: {p['comment']}"
        if r.get("distance") is not None:
            line += f"\n  Distance: {r['distance']:.1f} miles away"
        if p.get("list"):
            line += f"\n  List: {p['list']}"
        if p.get("url"):
            line += f"\n  Maps: {p['url']}"
        # Expert knowledge from shared DB
        if p.get("place_id"):
            expert = format_expert_knowledge(p["place_id"])
            if expert:
                line += f"\n{expert}"
        lines.append(line)

    return "\n\n".join(lines)


@mcp.tool()
async def get_place_info(place_name: str) -> str:
    """Get detailed, real-time information about a specific place.

    Returns current hours, whether it's open now, full address, phone,
    website, rating, reviews, and more. Works for both saved and unsaved places.

    Args:
        place_name: The name of the place to look up (e.g. "Joe's Pizza", "Blue Bottle Coffee Shibuya").
    """
    places = load_places()
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

    # Expert knowledge from shared DB
    if place_id:
        expert = format_expert_knowledge(place_id)
        if expert:
            lines.append(f"\nExpert notes:\n{expert}")

    if details.get("url"):
        lines.append(f"Google Maps: {details['url']}")

    return "\n".join(lines)


@mcp.tool()
async def discover_places(
    query: str,
    near: str = "",
    radius_miles: float = 2.0,
    include_saved: bool = True,
) -> str:
    """Discover new places beyond the user's saved list using Google Places search.

    IMPORTANT: Always call search_my_places FIRST. Only use this tool to
    supplement saved places with additional suggestions, or when the user
    explicitly asks for places they haven't saved.

    Args:
        query: What to search for (e.g. 'best pizza by the slice', 'trendy cocktail bar', 'late night ramen').
        near: Location to search near (e.g. 'Times Square NYC', 'Shibuya Tokyo', 'West Village'). Uses default location if not provided.
        radius_miles: Search radius in miles (default 2.0).
        include_saved: Whether to flag which results are already in your saved places (default True).
    """
    if not GOOGLE_PLACES_API_KEY:
        return "This tool requires a Google Places API key. Set the GOOGLE_PLACES_API_KEY environment variable."

    location = near or DEFAULT_LOCATION
    if not location:
        return "Please specify a location with the 'near' parameter, or set ROUX_DEFAULT_LOCATION."

    geo = await geocode_location(location)
    if not geo:
        return f"Could not find location: '{location}'. Try being more specific."

    radius_meters = int(radius_miles * 1609.34)
    results = await search_nearby_api(geo["lat"], geo["lng"], query=query, radius=radius_meters)

    if not results:
        results = await text_search_api(f"{query} near {location}")

    if not results:
        return f"No places found for '{query}' near {location}."

    saved_names = set()
    if include_saved:
        saved_names = {p["name"].lower() for p in load_places()}

    lines = [f"Found {len(results)} place(s) for '{query}' near {location}:\n"]
    for r in results[:10]:
        is_saved = r.get("name", "").lower() in saved_names
        name = r.get("name", "Unknown")
        line = f"**{name}**" + (" (saved)" if is_saved else "")

        if r.get("vicinity") or r.get("formatted_address"):
            line += f"\n  Address: {r.get('vicinity') or r.get('formatted_address')}"
        if r.get("rating"):
            price = "$" * r["price_level"] if r.get("price_level") else ""
            line += f"\n  Rating: {r['rating']}/5" + (f" | Price: {price}" if price else "")
        if r.get("opening_hours", {}).get("open_now") is not None:
            line += f"\n  Open now: {'Yes' if r['opening_hours']['open_now'] else 'No'}"
        if r.get("business_status") and r["business_status"] != "OPERATIONAL":
            line += f"\n  Status: {r['business_status']}"

        lines.append(line)

    return "\n\n".join(lines)


@mcp.tool()
async def add_note(place_name: str, note: str) -> str:
    """Add or update a note on one of your saved places.

    Args:
        place_name: The name of the saved place to annotate.
        note: The note to add (replaces any existing note).
    """
    places = load_places()
    matched = None
    for i, p in enumerate(places):
        if place_name.lower() in p.get("name", "").lower():
            matched = i
            break

    if matched is None:
        return f"No saved place matching '{place_name}'. Use search_my_places to find the exact name."

    places[matched]["note"] = note
    save_places(places)
    return f"Updated note for **{places[matched]['name']}**: \"{note}\""


@mcp.tool()
async def my_places_stats() -> str:
    """Get a summary of your saved places database.

    Shows total count, breakdown by list, top cuisines/types,
    and cities. Useful for understanding your taste profile.
    """
    places = load_places()
    if not places:
        return "No places in your database yet. Use import_places to get started."

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


@mcp.tool()
async def get_taste_profile() -> str:
    """Get the user's dining taste profile.

    Returns their preferences, patterns, deal-breakers, and dining style.
    Read this before making recommendations to personalize your suggestions.
    If no profile exists yet, suggest building one based on their saved places.
    """
    if not TASTE_PROFILE.exists():
        return "No taste profile yet. You can start one by telling me about your preferences, or I can analyze your saved places to generate an initial profile. Just say 'build my taste profile'."
    return TASTE_PROFILE.read_text()


@mcp.tool()
async def enrich_place_expert(place_name: str) -> str:
    """Fetch expert reviews and dish recommendations for a saved place from trusted editorial sources.

    Searches The Infatuation, Eater, Grub Street, NYT, and other approved sources,
    extracts what to order, what to skip, and stores it in the shared expert database.
    Results will appear in future searches and get_place_info calls.

    Args:
        place_name: Name of the saved place to enrich (e.g. "Peter Luger", "Don Angie").
    """
    places = load_places()
    matched = next((p for p in places if place_name.lower() in p.get("name", "").lower()), None)
    if not matched:
        return f"No saved place matching '{place_name}'. Use search_my_places to find the exact name."

    if not matched.get("place_id"):
        return f"'{matched['name']}' doesn't have a Google Place ID yet. Try importing again with a fresh Takeout export."

    try:
        from enrichment import enrich_one_place
        success = await enrich_one_place(matched)
        if success:
            expert = format_expert_knowledge(matched["place_id"])
            if expert:
                return f"Enriched **{matched['name']}**:\n{expert}"
            return f"Enriched **{matched['name']}** — no editorial coverage found yet."
        return f"Could not enrich **{matched['name']}** — check that ANTHROPIC_API_KEY is set and the place has editorial coverage."
    except Exception as e:
        return f"Enrichment error: {e}"


@mcp.tool()
async def update_taste_profile(updates: str) -> str:
    """Update the user's dining taste profile with new preferences or feedback.

    Call this when the user shares dining preferences, meal feedback, or
    patterns you've observed. The profile is a markdown document — append
    or edit sections as needed.

    Args:
        updates: The complete updated taste profile content (markdown). This replaces the existing profile, so include all existing content plus your changes.
    """
    ensure_data_dir()
    TASTE_PROFILE.write_text(updates)
    return "Taste profile updated."


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
