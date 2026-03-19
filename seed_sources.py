"""
Seed the sources table with approved editorial outlets.
Run once after schema creation: uv run python seed_sources.py
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))
import db


SOURCES = [
    # ── NYC: Institutional ──────────────────────────────────────────────────
    {
        "name": "The New York Times",
        "url": "https://www.nytimes.com/section/food",
        "scope": "city",
        "city": "New York",
        "type": "institutional",
        "quality_rank": 1,
        "crawl_strategy": "headlines_only",
        "approved": True,
    },
    {
        "name": "The Lo-Down / Ryan Sutton",
        "url": "https://ryansutton.substack.com",
        "scope": "city",
        "city": "New York",
        "type": "substack",
        "quality_rank": 2,
        "crawl_strategy": "free_tier",
        "approved": True,
    },
    {
        "name": "Robert Sietsema's New York",
        "url": "https://sietsema.substack.com",
        "scope": "city",
        "city": "New York",
        "type": "substack",
        "quality_rank": 3,
        "crawl_strategy": "free_tier",
        "approved": True,
    },
    {
        "name": "The New Yorker / Helen Rosner",
        "url": "https://www.newyorker.com/tag/food",
        "scope": "city",
        "city": "New York",
        "type": "institutional",
        "quality_rank": 4,
        "crawl_strategy": "headlines_only",
        "approved": True,
    },
    {
        "name": "Sweet City / Mahira Rivers",
        "url": "https://sweetcitynyc.substack.com",
        "scope": "city",
        "city": "New York",
        "type": "substack",
        "quality_rank": 5,
        "crawl_strategy": "free_tier",
        "approved": True,
    },
    # ── NYC: Editorial ──────────────────────────────────────────────────────
    {
        "name": "The Infatuation",
        "url": "https://www.theinfatuation.com/new-york",
        "scope": "city",
        "city": "New York",
        "type": "editorial",
        "quality_rank": 6,
        "crawl_strategy": "full",
        "approved": True,
    },
    {
        "name": "Grub Street",
        "url": "https://www.grubstreet.com",
        "scope": "city",
        "city": "New York",
        "type": "editorial",
        "quality_rank": 7,
        "crawl_strategy": "full",
        "approved": True,
    },
    {
        "name": "Eater New York",
        "url": "https://ny.eater.com",
        "scope": "city",
        "city": "New York",
        "type": "editorial",
        "quality_rank": 8,
        "crawl_strategy": "full",
        "approved": True,
    },
    {
        "name": "Resy Editorial / The Hit List",
        "url": "https://resy.com/cities/ny/hit-list",
        "scope": "city",
        "city": "New York",
        "type": "editorial",
        "quality_rank": 9,
        "crawl_strategy": "full",
        "approved": True,
    },
    # ── National: Editorial ─────────────────────────────────────────────────
    {
        "name": "Bon Appétit",
        "url": "https://www.bonappetit.com/restaurants",
        "scope": "national",
        "city": None,
        "type": "editorial",
        "quality_rank": 10,
        "crawl_strategy": "full",
        "approved": True,
    },
    {
        "name": "Tasting Table",
        "url": "https://www.tastingtable.com",
        "scope": "national",
        "city": None,
        "type": "editorial",
        "quality_rank": 11,
        "crawl_strategy": "full",
        "approved": True,
    },
    {
        "name": "Serious Eats",
        "url": "https://www.seriouseats.com",
        "scope": "national",
        "city": None,
        "type": "editorial",
        "quality_rank": 12,
        "crawl_strategy": "full",
        "approved": True,
    },
    {
        "name": "Time Out New York",
        "url": "https://www.timeout.com/newyork/restaurants",
        "scope": "city",
        "city": "New York",
        "type": "editorial",
        "quality_rank": 13,
        "crawl_strategy": "full",
        "approved": True,
    },
    # ── NYC: Substacks ──────────────────────────────────────────────────────
    {
        "name": "Eater New York (Substack)",
        "url": "https://eaternewsletter.substack.com",
        "scope": "city",
        "city": "New York",
        "type": "substack",
        "quality_rank": 14,
        "crawl_strategy": "free_tier",
        "approved": True,
    },
    {
        "name": "Broken Palate",
        "url": "https://brokenpalate.substack.com",
        "scope": "city",
        "city": "New York",
        "type": "substack",
        "quality_rank": 15,
        "crawl_strategy": "free_tier",
        "approved": True,
    },
    {
        "name": "Tap Is Fine!",
        "url": "https://tapisfine.substack.com",
        "scope": "city",
        "city": "New York",
        "type": "substack",
        "quality_rank": 16,
        "crawl_strategy": "free_tier",
        "approved": True,
    },
    {
        "name": "Amateur Gourmet",
        "url": "https://www.amateurgourmet.com",
        "scope": "city",
        "city": "New York",
        "type": "substack",
        "quality_rank": 17,
        "crawl_strategy": "full",
        "approved": True,
    },
    {
        "name": "Extra Credit / Alexis Benveniste",
        "url": "https://alexisbenveniste.substack.com",
        "scope": "city",
        "city": "New York",
        "type": "substack",
        "quality_rank": 18,
        "crawl_strategy": "free_tier",
        "approved": True,
    },
    # ── Social-first: passive only ──────────────────────────────────────────
    {
        "name": "Devour Power",
        "url": "https://www.instagram.com/devourpower",
        "scope": "city",
        "city": "New York",
        "type": "social",
        "quality_rank": 50,
        "crawl_strategy": "passive",
        "approved": False,  # Pending — social-first, limited web presence
    },
    {
        "name": "Brunch Boys",
        "url": "https://www.brunchboys.com",
        "scope": "city",
        "city": "New York",
        "type": "social",
        "quality_rank": 51,
        "crawl_strategy": "passive",
        "approved": False,
    },
    {
        "name": "Japanese Carlos",
        "url": "https://www.tiktok.com/@japanesecarlos",
        "scope": "city",
        "city": "New York",
        "type": "social",
        "quality_rank": 52,
        "crawl_strategy": "passive",
        "approved": False,
    },
    {
        "name": "5BoroughFoodie",
        "url": "https://www.instagram.com/5boroughfoodie",
        "scope": "city",
        "city": "New York",
        "type": "social",
        "quality_rank": 53,
        "crawl_strategy": "passive",
        "approved": False,
    },
    {
        "name": "One More Dish",
        "url": "https://www.instagram.com/onemoredish",
        "scope": "city",
        "city": "New York",
        "type": "social",
        "quality_rank": 54,
        "crawl_strategy": "passive",
        "approved": False,
    },
]


def seed():
    client = db.get_client()
    if not client:
        print("ERROR: No Supabase connection. Check .env for SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.")
        return

    print(f"Seeding {len(SOURCES)} sources...")
    result = client.table("sources").upsert(SOURCES, on_conflict="name").execute()
    print(f"Done. {len(result.data)} sources upserted.")

    # Print summary
    approved = [s for s in SOURCES if s["approved"]]
    pending = [s for s in SOURCES if not s["approved"]]
    print(f"  Approved and active: {len(approved)}")
    print(f"  Pending approval (social): {len(pending)}")


if __name__ == "__main__":
    seed()
