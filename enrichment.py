"""
Roux — Expert enrichment pipeline.

For each place in the user's saved list, searches approved editorial sources,
fetches review content, extracts structured dish data, and stores it in
the shared Supabase expert knowledge base.

Usage:
    uv run python enrichment.py                    # enrich all unenriched places
    uv run python enrichment.py --place "Joe's Pizza"  # enrich a specific place
    uv run python enrichment.py --all              # re-enrich everything
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

FIRST: Check if this article is actually about the restaurant "{name}" in {city}. If the article is about a DIFFERENT restaurant with a similar name, or about a restaurant in a different city, return: {{"wrong_match": true}}

If the article IS about the right restaurant, return a JSON object with these fields:
- "wrong_match": false
- "summary": 2-3 sentence summary of what makes this place worth going to (or not)
- "sentiment": "positive", "mixed", or "negative" overall sentiment
- "dishes": list of dish objects, each with:
    - "name": the simple, canonical dish name. Use the most common/menu name. Do NOT include brand names, restaurant names, or marketing language. Examples: "Smashburger" not "The Not a Damn Chance Burger", "French Fries" not "Beef Tallow Fries Beast Mode", "Chocolate Chip Cookie" not "Brown Butter Chocolate Chip Cookies". If two items are the same dish (e.g. "double burger" and "cheeseburger" at a place that only has one burger), merge them into one entry.
    - "sentiment": "must_order", "recommended", "skip", "overhyped", or "mixed"
    - "note": brief context ("lunch only", "seasonal", "not worth the hype", "wagyu beef, American cheese, secret sauce", etc.) — can be null
- "is_guide": true if this article mentions multiple restaurants (a guide/list), false if single-restaurant review
- "guide_theme": if is_guide=true, a short description like "Best Burgers in NYC" — else null

IMPORTANT: Keep the dish list tight. Only include genuinely distinct menu items. A place with 3 things on the menu should have ~3 dishes, not 10 variations.

Restaurant: {name}
City/Location: {city}
Article title: {title}
Article text:
{text}

Return only valid JSON, no other text."""


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


SITE_SEARCH_URLS = [
    # (domain_keyword, search_url_template)
    ("theinfatuation.com", "https://www.theinfatuation.com/new-york/search?query={query}"),
    ("eater.com",          "https://ny.eater.com/search?q={query}"),
    ("grubstreet.com",     "https://www.grubstreet.com/search?q={query}"),
    ("bonappetit.com",     "https://www.bonappetit.com/search?q={query}"),
    ("seriouseats.com",    "https://www.seriouseats.com/search?q={query}"),
    ("timeout.com",        "https://www.timeout.com/newyork/search?q={query}"),
    ("tastingtable.com",   "https://www.tastingtable.com/search/{query}"),
]


async def search_sources(place_name: str, city: str = "") -> list[dict]:
    """Search approved editorial sources directly for a place name."""
    import urllib.parse
    query = urllib.parse.quote_plus(place_name)
    results = []

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; RouxBot/1.0)"},
        timeout=10.0,
    ) as client:
        for domain_key, url_template in SITE_SEARCH_URLS:
            url = url_template.format(query=query)
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue
                html = resp.text

                # Extract links from search results that mention the place name
                import re
                # Find <a href> links that look like article/review URLs
                links = re.findall(r'href="(https?://[^"]+(?:review|guide|best|restaurant)[^"]*)"', html, re.IGNORECASE)
                # Also find links from the same domain
                domain_links = re.findall(rf'href="(https?://[^"]*{re.escape(domain_key.split(".")[0])}[^"]*)"', html, re.IGNORECASE)

                all_links = list(dict.fromkeys(links + domain_links))  # dedupe, preserve order

                # Filter to links likely about this place
                name_parts = place_name.lower().split()
                relevant = []
                for link in all_links[:10]:
                    link_lower = link.lower()
                    if any(part in link_lower for part in name_parts if len(part) > 3):
                        relevant.append({"url": link, "title": "", "domain": domain_key})

                if relevant:
                    results.extend(relevant[:2])  # Max 2 per source
                    logger.info(f"Found {len(relevant)} links for '{place_name}' on {domain_key}")

            except Exception as e:
                logger.debug(f"Search error for {domain_key}: {e}")

    return results


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


def extract_with_llm(place_name: str, title: str, text: str, city: str = "") -> dict | None:
    """Use Claude to extract structured dish/review data from article text."""
    if not ANTHROPIC_API_KEY:
        logger.warning("No ANTHROPIC_API_KEY — skipping LLM extraction")
        return None
    if len(text) < 100:
        return None

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = EXTRACTION_PROMPT.format(name=place_name, city=city or "unknown", title=title, text=text[:6000])

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",  # Fast and cheap for extraction
            max_tokens=1024,
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


async def enrich_one_place(place: dict) -> bool:
    """Enrich a single saved place with expert knowledge. Returns True if enriched."""
    name = place.get("name", "")
    place_id = place.get("place_id", "")
    address = place.get("address", "")
    city = place.get("address", "").split(",")[-3].strip() if place.get("address") else ""

    if not name or not place_id:
        logger.info(f"Skipping {name} — no Google Place ID")
        return False

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
        "website": place.get("website", ""),
        "phone": place.get("phone", ""),
    }
    stored = db.upsert_expert_place(expert_record)
    if not stored:
        logger.error(f"Failed to upsert expert place: {name}")
        return False

    expert_id = stored["id"]

    # Skip full enrichment if already enriched recently
    if existing and existing.get("last_enriched_at"):
        logger.info(f"Already enriched: {name}")
        return True

    logger.info(f"Enriching: {name}")

    # Search for reviews — include city to avoid wrong-restaurant matches
    sites = " OR ".join(f"site:{d}" for d in SOURCE_DOMAINS)
    location_hint = city or address.split(",")[0] if address else ""
    query = f'"{name}" {location_hint} ({sites})'
    search_results = web_search(query, num=5)
    if search_results:
        # Filter to only approved domains, cap at 3 (diminishing returns after that)
        search_results = [r for r in search_results if any(d in r.get("url", "") for d in SOURCE_DOMAINS)][:3]
        logger.info(f"  Processing {len(search_results)} editorial results")

    if not search_results:
        logger.info(f"No search results for {name}")
        # Mark as enriched (even if no results) so we don't retry every time
        db.upsert_expert_place({**expert_record, "last_enriched_at": "now()"})
        return True

    enriched_any = False
    all_extracted_dishes = []
    for result in search_results:
        url = result.get("url", "")
        if not url:
            continue

        # Only process URLs from approved sources
        if not any(domain in url for domain in SOURCE_DOMAINS):
            continue

        source_id = get_source_id_for_url(url)
        if not source_id:
            continue

        title, text = await fetch_article(url)
        if not text:
            continue

        extracted = extract_with_llm(name, title, text, city=city)
        if not extracted:
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
            "raw_text": text[:2000],  # Store truncated version
        }
        stored_review = db.upsert_review(review)
        if not stored_review:
            continue

        review_id = stored_review["id"]

        # Collect dishes for batch dedup after all articles processed
        dishes = extracted.get("dishes", [])
        for d in dishes:
            if d.get("name"):
                all_extracted_dishes.append({
                    "expert_place_id": expert_id,
                    "source_id": source_id,
                    "review_id": review_id,
                    "dish_name": d.get("name", ""),
                    "sentiment": d.get("sentiment", "recommended"),
                    "note": d.get("note"),
                })

        # If this is a guide, capture other places mentioned
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
                db.upsert_guide_mention({
                    "guide_id": stored_guide["id"],
                    "expert_place_id": expert_id,
                    "context": extracted.get("summary", ""),
                })

        enriched_any = True

    # Batch dedup and write all dishes at once
    if all_extracted_dishes:
        db.batch_upsert_dishes(expert_id, all_extracted_dishes)

    # Mark as enriched
    db.upsert_expert_place({**expert_record, "last_enriched_at": "now()"})
    return enriched_any


async def run_enrichment(filter_name: str | None = None, force: bool = False, user_id: str | None = None):
    """Run enrichment pipeline across saved places."""
    if not db.get_client():
        logger.error("No Supabase connection — check SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
        return

    # Load places for a specific user, or all users
    if user_id:
        places = db.load_user_places(user_id)
    else:
        # Enrich places for all users
        client = db.get_client()
        all_users = client.table("users").select("id").execute()
        places = []
        for u in (all_users.data or []):
            places.extend(db.load_user_places(u["id"]))
    if filter_name:
        places = [p for p in places if filter_name.lower() in p.get("name", "").lower()]
        logger.info(f"Filtered to {len(places)} places matching '{filter_name}'")
    else:
        if not force:
            # Only process places not yet in expert DB
            unenriched = []
            for p in places:
                if p.get("place_id") and not db.get_expert_place(p["place_id"]):
                    unenriched.append(p)
            places = unenriched
        logger.info(f"Enriching {len(places)} places")

    success = 0
    for i, place in enumerate(places):
        logger.info(f"[{i+1}/{len(places)}] {place.get('name')}")
        try:
            if await enrich_one_place(place):
                success += 1
        except Exception as e:
            logger.error(f"Error enriching {place.get('name')}: {e}")
        await asyncio.sleep(2)  # Be respectful to search engine

    logger.info(f"Enrichment complete: {success}/{len(places)} places enriched")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Roux enrichment pipeline")
    parser.add_argument("--place", help="Enrich a specific place by name")
    parser.add_argument("--all", action="store_true", help="Re-enrich all places")
    parser.add_argument("--user-id", help="Enrich places for a specific user ID")
    args = parser.parse_args()

    asyncio.run(run_enrichment(filter_name=args.place, force=args.all, user_id=args.user_id))
