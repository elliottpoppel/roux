"""
Roux — Expert enrichment pipeline.

For each place in the user's saved list, searches approved editorial sources,
fetches review content, extracts structured dish data, and stores it in
the shared Supabase expert knowledge base.

Usage:
    uv run python enrichment.py                    # enrich unenriched + outdated places
    uv run python enrichment.py --place "Joe's Pizza"  # enrich a specific place
    uv run python enrichment.py --all              # re-enrich everything (force)
    uv run python enrichment.py --upgrade          # re-enrich only outdated pipeline versions
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

import httpx
import trafilatura
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

# Add parent dir to path so we can import db
sys.path.insert(0, str(Path(__file__).parent))
import db

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("roux.enrichment")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")

# Bump this when the pipeline changes meaningfully (new sources, prompt changes,
# search strategy). Places enriched with an older version will be re-enriched
# automatically on the next run.
PIPELINE_VERSION = 2

# Google Place types that indicate a dining establishment.
DINING_TYPES = {
    "restaurant", "cafe", "bar", "bakery", "meal_delivery",
    "meal_takeaway", "food", "night_club",
}


def is_dining_place(types: list[str]) -> bool:
    """Return True if the place types indicate a dining establishment."""
    return bool(set(types) & DINING_TYPES)

# Sources to search per place (in priority order)
SOURCE_DOMAINS = [
    "nytimes.com",
    "guide.michelin.com",
    "theinfatuation.com",
    "grubstreet.com",
    "eater.com",
    "bonappetit.com",
    "tastingtable.com",
    "seriouseats.com",
    "timeout.com",
    "thrillist.com",
]

EXTRACTION_PROMPT = """You are extracting structured data from a restaurant review or guide.

FIRST: Check if this article is actually about the restaurant "{name}" at {address}.{website_hint} The article must be about this EXACT restaurant — not a different restaurant with a similar name, not a different location/branch, not a recipe. If it's not about this specific place, return: {{"wrong_match": true}}

If the article IS about the right restaurant, return a JSON object with these fields:
- "wrong_match": false
- "summary": 2-3 sentence summary of what makes this place worth going to (or not)
- "sentiment": "positive", "mixed", or "negative" overall sentiment
- "dishes": list of dish objects for "{name}", each with:
    - "name": the simple, canonical dish name. Use the most common/menu name. Do NOT include brand names, restaurant names, or marketing language. Examples: "Smashburger" not "The Not a Damn Chance Burger", "French Fries" not "Beef Tallow Fries Beast Mode", "Chocolate Chip Cookie" not "Brown Butter Chocolate Chip Cookies". If two items are the same dish (e.g. "double burger" and "cheeseburger" at a place that only has one burger), merge them into one entry.
    - "sentiment": "must_order", "recommended", "skip", "overhyped", or "mixed"
    - "note": brief context ("lunch only", "seasonal", "not worth the hype", "wagyu beef, American cheese, secret sauce", etc.) — can be null
- "is_guide": true if this article mentions multiple restaurants (a guide/list), false if single-restaurant review
- "guide_theme": if is_guide=true, a short description like "Best Burgers in NYC" — else null
- "guide_places": if is_guide=true, a list of ALL OTHER restaurants mentioned in the article (not "{name}"), each with:
    - "name": restaurant name
    - "neighborhood": neighborhood or city (e.g. "Greenwich Village", "Williamsburg", "San Francisco")
    - "dishes": list of dish objects (same format as above) — include dishes mentioned for this place. Can be empty if none mentioned.
    - "context": one sentence on why this place is notable in the guide — can be null

IMPORTANT: Keep dish lists tight. Only include genuinely distinct menu items.
IMPORTANT: For guide_places, include EVERY restaurant mentioned in the article, not just the highlights.

Restaurant: {name}
Address: {address}
Website: {website}
Article title: {title}
Article text:
{text}

Return only valid JSON, no other text."""


def resolve_place_by_name(name: str, neighborhood: str = "") -> dict | None:
    """Resolve a restaurant name to Google Places data via Find Place API.

    Returns dict with place_id, name, address, lat, lng, types, rating, or None.
    """
    if not GOOGLE_API_KEY or not name:
        return None

    query = f"{name} {neighborhood}".strip() if neighborhood else name
    try:
        resp = httpx.get(
            "https://maps.googleapis.com/maps/api/place/findplacefromtext/json",
            params={
                "input": query,
                "inputtype": "textquery",
                "fields": "place_id,name,formatted_address,geometry,types,price_level,rating,business_status",
                "key": GOOGLE_API_KEY,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        candidates = resp.json().get("candidates", [])
        if not candidates:
            return None

        c = candidates[0]
        loc = c.get("geometry", {}).get("location", {})
        return {
            "place_id": c.get("place_id", ""),
            "name": c.get("name", name),
            "address": c.get("formatted_address", ""),
            "lat": loc.get("lat"),
            "lng": loc.get("lng"),
            "types": c.get("types", []),
            "price_level": c.get("price_level"),
            "rating": c.get("rating"),
            "business_status": c.get("business_status"),
        }
    except Exception as e:
        logger.warning(f"Failed to resolve place '{name}': {e}")
        return None


def web_search(query: str, num: int = 5) -> list[dict]:
    """Search using Brave Search API — free tier, 2000 queries/month."""
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "")
    if not api_key:
        logger.warning("No BRAVE_SEARCH_API_KEY set — skipping search")
        return []

    try:
        resp = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": num},
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
            timeout=10.0,
        )
        resp.raise_for_status()
        results = resp.json().get("web", {}).get("results", [])
        return [{"title": r.get("title", ""), "url": r.get("url", ""),
                 "snippet": r.get("description", "")} for r in results]
    except Exception as e:
        logger.error(f"Brave Search error: {e}")
    return []


async def fetch_article(url: str) -> tuple[str, str]:
    """Fetch a URL and extract readable article text. Returns (title, text)."""
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; RouxBot/1.0)"},
        timeout=15.0,
    ) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text

            # Use trafilatura for clean text extraction
            text = trafilatura.extract(html, include_comments=False,
                                       include_tables=False, no_fallback=False)
            title = ""
            # Extract title from HTML
            title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
            if title_match:
                title = re.sub(r"\s+", " ", title_match.group(1)).strip()

            return title, (text or "")[:8000]  # Cap at 8k chars for LLM
        except Exception as e:
            logger.warning(f"Failed to fetch {url}: {e}")
            return "", ""


def extract_with_llm(place_name: str, title: str, text: str, address: str = "", website: str = "") -> dict | None:
    """Use Claude to extract structured dish/review data from article text."""
    if not ANTHROPIC_API_KEY:
        logger.warning("No ANTHROPIC_API_KEY — skipping LLM extraction")
        return None
    if len(text) < 100:
        return None

    website_hint = f" The restaurant's website is {website}." if website else ""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = EXTRACTION_PROMPT.format(
        name=place_name, address=address or "unknown",
        website_hint=website_hint, website=website or "unknown",
        title=title, text=text[:6000],
    )

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",  # Fast and cheap for extraction
            max_tokens=2048,  # Guides with many places need more output
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        logger.error(f"LLM extraction error: {e}")
        return None


def get_source_id_for_url(url: str) -> str | None:
    """Find the best matching source in the DB for a given URL."""
    domain_to_name = {
        "theinfatuation.com": "The Infatuation",
        "eater.com": "Eater New York",
        "grubstreet.com": "Grub Street",
        "nytimes.com": "The New York Times",
        "guide.michelin.com": "Michelin Guide",
        "bonappetit.com": "Bon Appétit",
        "tastingtable.com": "Tasting Table",
        "seriouseats.com": "Serious Eats",
        "timeout.com": "Time Out New York",
        "thrillist.com": "Thrillist",
    }
    for domain, name in domain_to_name.items():
        if domain in url:
            source = db.get_source_by_name(name)
            if source:
                return source["id"]
    return None


async def enrich_one_place(place: dict, force: bool = False) -> bool:
    """Enrich a single saved place with expert knowledge. Returns True if enriched."""
    name = place.get("name", "")
    place_id = place.get("place_id", "")
    address = place.get("address", "")
    city = place.get("address", "").split(",")[-3].strip() if place.get("address") else ""
    website = place.get("website", "")
    google_name = name  # May be overridden by Google Places full name

    if not name or not place_id:
        logger.info(f"Skipping {name} — no Google Place ID")
        return False

    # Skip non-dining places
    if not is_dining_place(place.get("types", [])):
        logger.info(f"Skipping {name} — not a dining place (types: {place.get('types', [])})")
        return False

    # Only call Google Places Details API if we're missing the website
    # (import already stored name, address, types, rating, etc.)
    if not website:
        try:
            from server import get_place_details_api
            details = await get_place_details_api(place_id)
            if details:
                google_name = details.get("name", name)
                website = details.get("website", website)
                if google_name != name:
                    logger.info(f"  Google name: {google_name}")
        except Exception:
            pass

    # Check if already in expert DB
    existing = db.get_expert_place(place_id)

    # Upsert expert place record
    expert_record = {
        "google_place_id": place_id,
        "name": name,
        "address": address,
        "city": city,
        "lat": place.get("lat"),
        "lng": place.get("lng"),
        "place_types": place.get("types", []),
        "price_level": place.get("price_level"),
        "google_rating": place.get("rating"),
        "website": website,
        "phone": place.get("phone", ""),
    }
    stored = db.upsert_expert_place(expert_record)
    if not stored:
        logger.error(f"Failed to upsert expert place: {name}")
        return False

    expert_id = stored["id"]

    # Skip if already enriched with current pipeline version (unless forced)
    if not force and existing:
        existing_version = existing.get("pipeline_version", 0) or 0
        if existing_version >= PIPELINE_VERSION:
            logger.info(f"Already enriched (v{existing_version}): {name}")
            return True

    logger.info(f"Enriching: {name}")

    # Search editorial sources — try multiple strategies for best results
    sites = " OR ".join(f"site:{d}" for d in SOURCE_DOMAINS)
    location_hint = city or address.split(",")[0] if address else ""
    # Strategy 1: Google's full name (if more specific than saved name)
    # Strategy 2: Saved name + location
    # Strategy 3: Saved name + street (for generic names like "Saint")
    search_queries = []
    if google_name != name and len(google_name) > len(name):
        search_queries.append(f'"{google_name}" ({sites})')
    search_queries.append(f'"{name}" {location_hint} ({sites})')
    street = address.split(",")[0].strip() if address else ""
    if len(name) < 10 and street:
        search_queries.append(f'"{name}" "{street}" ({sites})')

    search_results = []
    for query in search_queries:
        search_results = web_search(query, num=5)
        if search_results:
            search_results = [r for r in search_results if any(d in r.get("url", "") for d in SOURCE_DOMAINS)][:3]
        if search_results:
            break  # Got results, stop trying

    if search_results:
        logger.info(f"  Processing {len(search_results)} editorial results")
    else:
        logger.info(f"No search results for {name}")
        # Mark as enriched (even if no results) so we don't retry every time
        db.upsert_expert_place({**expert_record, "last_enriched_at": "now()", "pipeline_version": PIPELINE_VERSION})
        return True

    enriched_any = False
    all_extracted_dishes = []

    # Build list of articles to process (filter to approved sources)
    to_fetch = []
    for result in search_results:
        url = result.get("url", "")
        if not url or not any(domain in url for domain in SOURCE_DOMAINS):
            continue
        source_id = get_source_id_for_url(url)
        if source_id:
            to_fetch.append((url, source_id))

    # Fetch all articles concurrently
    fetch_results = await asyncio.gather(
        *[fetch_article(url) for url, _ in to_fetch],
        return_exceptions=True,
    )

    # Extract structured data from all articles concurrently (LLM calls)
    extract_tasks = []
    fetched_articles = []
    for (url, source_id), result in zip(to_fetch, fetch_results):
        if isinstance(result, Exception) or not result[1]:
            continue
        title, text = result
        fetched_articles.append((url, source_id, title, text))
        extract_tasks.append(
            asyncio.to_thread(extract_with_llm, name, title, text, address, website)
        )

    extracted_results = await asyncio.gather(*extract_tasks, return_exceptions=True)

    # Process results and write to DB
    for (url, source_id, title, text), extracted in zip(fetched_articles, extracted_results):
        if isinstance(extracted, Exception) or not extracted:
            continue
        if extracted.get("wrong_match"):
            logger.info(f"  Skipping wrong match: {title[:60]}")
            continue

        # Store the review
        review = {
            "expert_place_id": expert_id,
            "source_id": source_id,
            "url": url,
            "title": title,
            "sentiment": extracted.get("sentiment"),
            "summary": extracted.get("summary"),
            "raw_text": text[:2000],
        }
        stored_review = db.upsert_review(review)
        if not stored_review:
            continue

        review_id = stored_review["id"]

        # Collect dishes for batch dedup after all articles processed
        for d in extracted.get("dishes", []):
            if d.get("name"):
                all_extracted_dishes.append({
                    "expert_place_id": expert_id,
                    "source_id": source_id,
                    "review_id": review_id,
                    "dish_name": d.get("name", ""),
                    "sentiment": d.get("sentiment", "recommended"),
                    "note": d.get("note"),
                })

        # If this is a guide, capture the primary place and all other places mentioned
        if extracted.get("is_guide") and extracted.get("guide_theme"):
            guide_record = {
                "source_id": source_id,
                "url": url,
                "title": title,
                "theme": extracted["guide_theme"],
                "city": city,
                "scope": "city" if city else "national",
            }
            stored_guide = db.upsert_guide(guide_record)
            if stored_guide:
                # Link primary place to guide
                db.upsert_guide_mention({
                    "guide_id": stored_guide["id"],
                    "expert_place_id": expert_id,
                    "context": extracted.get("summary", ""),
                })

                # Process all other places mentioned in the guide
                for gp in extracted.get("guide_places", []):
                    gp_name = gp.get("name", "").strip()
                    if not gp_name:
                        continue

                    # Resolve via Google Places API
                    resolved = resolve_place_by_name(gp_name, gp.get("neighborhood", city))
                    if not resolved or not resolved.get("place_id"):
                        logger.debug(f"  Guide place not resolved: {gp_name}")
                        continue

                    # Skip non-dining places
                    if not is_dining_place(resolved.get("types", [])):
                        continue

                    # Upsert expert_place (don't set last_enriched_at — this is partial data)
                    gp_record = {
                        "google_place_id": resolved["place_id"],
                        "name": resolved["name"],
                        "address": resolved.get("address", ""),
                        "city": gp.get("neighborhood", city),
                        "lat": resolved.get("lat"),
                        "lng": resolved.get("lng"),
                        "place_types": resolved.get("types", []),
                        "price_level": resolved.get("price_level"),
                        "google_rating": resolved.get("rating"),
                    }
                    stored_gp = db.upsert_expert_place(gp_record)
                    if not stored_gp:
                        continue

                    gp_expert_id = stored_gp["id"]

                    # Link to guide
                    db.upsert_guide_mention({
                        "guide_id": stored_guide["id"],
                        "expert_place_id": gp_expert_id,
                        "context": gp.get("context", ""),
                    })

                    # Store dishes
                    gp_dishes = []
                    for d in gp.get("dishes", []):
                        if d.get("name"):
                            gp_dishes.append({
                                "expert_place_id": gp_expert_id,
                                "source_id": source_id,
                                "review_id": review_id,
                                "dish_name": d.get("name", ""),
                                "sentiment": d.get("sentiment", "recommended"),
                                "note": d.get("note"),
                            })
                    if gp_dishes:
                        db.batch_upsert_dishes(gp_expert_id, gp_dishes)

                    logger.info(f"  Guide place: {gp_name} → {resolved['name']} ({len(gp.get('dishes', []))} dishes)")
                    await asyncio.sleep(0.2)  # Rate limit Google API calls

        enriched_any = True

    # Batch dedup and write all dishes at once
    if all_extracted_dishes:
        db.batch_upsert_dishes(expert_id, all_extracted_dishes)

    # Mark as enriched with current pipeline version
    db.upsert_expert_place({**expert_record, "last_enriched_at": "now()", "pipeline_version": PIPELINE_VERSION})
    return enriched_any


def _enrichment_priority(place: dict, home_city: str = "") -> tuple:
    """Sort key for enrichment priority (lower = higher priority)."""
    # Places with food-related user notes first
    note = f"{place.get('note', '')} {place.get('comment', '')}".lower()
    food_keywords = {"order", "try", "dish", "burger", "pizza", "taco", "ramen",
                     "sushi", "pasta", "cocktail", "wine", "delicious", "amazing",
                     "must", "best", "favorite", "recommend"}
    has_food_note = 0 if any(kw in note for kw in food_keywords) else 1

    # Home city places next
    city_match = 0 if home_city and home_city.lower() in place.get("address", "").lower() else 1

    # Higher-rated places first (more editorial coverage)
    rating = -(place.get("rating") or 0)

    return (has_food_note, city_match, rating)


async def run_enrichment(filter_name: str | None = None, force: bool = False,
                         user_id: str | None = None, upgrade: bool = False):
    """Run enrichment pipeline across saved places."""
    if not db.get_client():
        logger.error("No Supabase connection — check SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
        return

    # Load places for a specific user, or all users
    if user_id:
        places = db.load_user_places(user_id)
    else:
        client = db.get_client()
        all_users = client.table("users").select("id").execute()
        places = []
        for u in (all_users.data or []):
            places.extend(db.load_user_places(u["id"]))

    # Filter to dining places only
    total_before = len(places)
    places = [p for p in places if p.get("place_id") and is_dining_place(p.get("types", []))]
    skipped = total_before - len(places)
    if skipped:
        logger.info(f"Skipped {skipped} non-dining places")

    if filter_name:
        places = [p for p in places if filter_name.lower() in p.get("name", "").lower()]
        logger.info(f"Filtered to {len(places)} places matching '{filter_name}'")
    elif not force:
        # Filter to places that need enrichment: unenriched OR outdated pipeline version
        needs_enrichment = []
        for p in places:
            existing = db.get_expert_place(p["place_id"])
            if not existing:
                needs_enrichment.append(p)
            elif upgrade or (existing.get("pipeline_version", 0) or 0) < PIPELINE_VERSION:
                needs_enrichment.append(p)
        places = needs_enrichment

    logger.info(f"Enriching {len(places)} dining places (pipeline v{PIPELINE_VERSION})")

    # Sort by priority: food notes first, then home city, then by rating
    home_city = ""
    if user_id:
        locations = db.get_user_locations(user_id)
        home_city = locations.get("home", "")
    places.sort(key=lambda p: _enrichment_priority(p, home_city))

    success = 0
    for i, place in enumerate(places):
        logger.info(f"[{i+1}/{len(places)}] {place.get('name')}")
        try:
            if await enrich_one_place(place, force=force):
                success += 1
        except Exception as e:
            logger.error(f"Error enriching {place.get('name')}: {e}")
        await asyncio.sleep(2)  # Be respectful to search engine

    logger.info(f"Enrichment complete: {success}/{len(places)} places enriched")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Roux enrichment pipeline")
    parser.add_argument("--place", help="Enrich a specific place by name")
    parser.add_argument("--all", action="store_true", help="Re-enrich all places (force)")
    parser.add_argument("--upgrade", action="store_true", help="Re-enrich only places with outdated pipeline version")
    parser.add_argument("--user-id", help="Enrich places for a specific user ID")
    args = parser.parse_args()

    asyncio.run(run_enrichment(
        filter_name=args.place, force=args.all,
        user_id=args.user_id, upgrade=args.upgrade,
    ))
