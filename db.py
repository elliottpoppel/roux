"""
Roux — Supabase database client.
Handles all reads/writes to the shared expert knowledge base.
"""

import os
import logging
from typing import Any

from supabase import create_client, Client

logger = logging.getLogger("roux.db")

_client: Client | None = None


def get_client() -> Client | None:
    global _client
    if _client is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if url and key:
            _client = create_client(url, key)
    return _client


# ---------------------------------------------------------------------------
# Expert places
# ---------------------------------------------------------------------------


def get_expert_place(google_place_id: str) -> dict | None:
    db = get_client()
    if not db:
        return None
    result = db.table("expert_places").select("*").eq("google_place_id", google_place_id).execute()
    return result.data[0] if result.data else None


def upsert_expert_place(place: dict) -> dict | None:
    """Insert or update an expert place. Returns the stored record."""
    db = get_client()
    if not db:
        return None
    result = db.table("expert_places").upsert(place, on_conflict="google_place_id").execute()
    return result.data[0] if result.data else None


# ---------------------------------------------------------------------------
# Reviews and dishes
# ---------------------------------------------------------------------------


def get_place_reviews(expert_place_id: str) -> list[dict]:
    db = get_client()
    if not db:
        return []
    result = (
        db.table("place_reviews")
        .select("*, sources(name, quality_rank)")
        .eq("expert_place_id", expert_place_id)
        .order("sources(quality_rank)")
        .execute()
    )
    return result.data or []


def get_place_dishes(expert_place_id: str) -> list[dict]:
    db = get_client()
    if not db:
        return []
    result = (
        db.table("place_dishes")
        .select("*, sources(name, quality_rank)")
        .eq("expert_place_id", expert_place_id)
        .order("sources(quality_rank)")
        .execute()
    )
    return result.data or []


def upsert_review(review: dict) -> dict | None:
    db = get_client()
    if not db:
        return None
    result = db.table("place_reviews").upsert(
        review, on_conflict="expert_place_id,source_id,url"
    ).execute()
    return result.data[0] if result.data else None


def insert_dishes(dishes: list[dict]):
    db = get_client()
    if not db or not dishes:
        return
    db.table("place_dishes").insert(dishes).execute()


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def get_approved_sources(city: str | None = None) -> list[dict]:
    db = get_client()
    if not db:
        return []
    q = db.table("sources").select("*").eq("approved", True).eq("active", True)
    if city:
        q = q.or_(f"city.eq.{city},scope.in.(national,global)")
    return q.order("quality_rank").execute().data or []


def get_source_by_name(name: str) -> dict | None:
    db = get_client()
    if not db:
        return None
    result = db.table("sources").select("*").ilike("name", name).execute()
    return result.data[0] if result.data else None


# ---------------------------------------------------------------------------
# Guides
# ---------------------------------------------------------------------------


def upsert_guide(guide: dict) -> dict | None:
    db = get_client()
    if not db:
        return None
    result = db.table("guides").upsert(guide, on_conflict="url").execute()
    return result.data[0] if result.data else None


def upsert_guide_mention(mention: dict):
    db = get_client()
    if not db:
        return
    db.table("guide_mentions").upsert(
        mention, on_conflict="guide_id,expert_place_id"
    ).execute()


# ---------------------------------------------------------------------------
# Expert knowledge for a place (consolidated view for MCP tools)
# ---------------------------------------------------------------------------


def get_expert_knowledge(google_place_id: str) -> dict | None:
    """Return all expert knowledge for a place: reviews, dishes, guide mentions."""
    db = get_client()
    if not db:
        return None

    expert = get_expert_place(google_place_id)
    if not expert:
        return None

    expert_id = expert["id"]
    reviews = get_place_reviews(expert_id)
    dishes = get_place_dishes(expert_id)

    # Guide mentions
    mentions_result = (
        db.table("guide_mentions")
        .select("context, rank, guides(title, url, theme, source_id)")
        .eq("expert_place_id", expert_id)
        .execute()
    )
    mentions = mentions_result.data or []

    return {
        "place": expert,
        "reviews": reviews,
        "dishes": dishes,
        "guide_mentions": mentions,
    }


def search_expert_by_dish(dish_query: str, city: str | None = None) -> list[dict]:
    """Find expert places known for a specific dish or drink."""
    db = get_client()
    if not db:
        return []

    q = (
        db.table("place_dishes")
        .select("*, expert_places(*), sources(name, quality_rank)")
        .textSearch("dish_name", dish_query, config="english")
        .in_("sentiment", ["must_order", "recommended"])
        .order("sources(quality_rank)")
        .limit(20)
    )
    results = q.execute().data or []

    if city:
        results = [r for r in results if r.get("expert_places", {}).get("city", "").lower() == city.lower()]

    return results
