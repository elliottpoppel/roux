"""
Roux — Supabase database client.
Handles all reads/writes to the shared expert knowledge base
and per-user data (places, taste profiles).
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
        logger.info(f"Supabase URL: {url[:30] + '...' if url else 'NOT SET'}")
        logger.info(f"Supabase key: {'SET (' + key[:10] + '...)' if key else 'NOT SET'}")
        if url and key:
            _client = create_client(url, key)
            logger.info("Supabase client created")
    return _client


# ---------------------------------------------------------------------------
# User identity
# ---------------------------------------------------------------------------


def get_or_create_user(client_id: str) -> str:
    """Return a stable user_id for the given OAuth client_id, creating if needed."""
    client = get_client()
    if not client:
        return "local"

    # Check if this client_id is already mapped
    result = client.table("user_clients").select("user_id").eq("client_id", client_id).execute()
    if result.data:
        return result.data[0]["user_id"]

    # Unknown client_id — check if there's only one user (single-tenant mode)
    all_users = client.table("users").select("id").execute()
    if all_users.data and len(all_users.data) == 1:
        # Single user — auto-link this client_id to them
        user_id = all_users.data[0]["id"]
        logger.info(f"Auto-linking client {client_id} to sole user {user_id}")
        try:
            client.table("user_clients").insert({
                "user_id": user_id,
                "client_id": client_id,
            }).execute()
        except Exception as e:
            logger.warning(f"Failed to save client mapping: {e}")
        return user_id

    # Multi-user: create a new user
    try:
        user = client.table("users").insert({"display_name": None}).execute()
        user_id = user.data[0]["id"]
        client.table("user_clients").insert({"user_id": user_id, "client_id": client_id}).execute()
        logger.info(f"Created new user {user_id} for client {client_id}")
        return user_id
    except Exception as e:
        logger.error(f"get_or_create_user failed: {e}")
        return "local"


# ---------------------------------------------------------------------------
# User places (per-user, private)
# ---------------------------------------------------------------------------


def load_user_places(user_id: str, list_name: str | None = None) -> list[dict]:
    """Load all saved places for a user, optionally filtered by list."""
    db = get_client()
    if not db:
        return []
    q = db.table("user_places").select("*").eq("user_id", user_id)
    if list_name:
        q = q.eq("list", list_name)
    return q.execute().data or []


def upsert_user_places(user_id: str, places: list[dict]):
    """Insert or update places for a user. Dedup by (user_id, name, url)."""
    db = get_client()
    if not db:
        return
    for p in places:
        p["user_id"] = user_id
    db.table("user_places").upsert(places, on_conflict="user_id,name,url").execute()


def update_user_place(place_id: str, updates: dict):
    """Update a single user place by its row ID."""
    db = get_client()
    if not db:
        return
    db.table("user_places").update(updates).eq("id", place_id).execute()


def delete_user_places(user_id: str, place_ids: list[str]):
    """Remove places by their row IDs, scoped to user."""
    db = get_client()
    if not db or not place_ids:
        return
    db.table("user_places").delete().in_("id", place_ids).eq("user_id", user_id).execute()


# ---------------------------------------------------------------------------
# User taste profiles (per-user, private)
# ---------------------------------------------------------------------------


def get_user_taste_profile(user_id: str) -> str | None:
    """Get the taste profile content for a user."""
    db = get_client()
    if not db:
        return None
    result = db.table("user_taste_profiles").select("content").eq("user_id", user_id).execute()
    return result.data[0]["content"] if result.data else None


def upsert_user_taste_profile(user_id: str, content: str):
    """Create or update a user's taste profile."""
    db = get_client()
    if not db:
        return
    db.table("user_taste_profiles").upsert(
        {"user_id": user_id, "content": content}, on_conflict="user_id"
    ).execute()


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


def _normalize_dish_name(name: str) -> str:
    """Normalize a dish name for dedup comparison.

    Strips modifiers like 'wagyu', 'double', 'beef tallow' so that
    'Wagyu Smash Burger' and 'Smashburger' match as the same dish.
    """
    import re
    name = name.strip().lower()
    # Remove common modifiers that don't change the core dish
    modifiers = [
        r'\bwagyu\b', r'\bdouble\b', r'\bsingle\b', r'\bbeef tallow\b',
        r'\bbrown butter\b', r'\bhouse[- ]?made\b', r'\btexas\b',
        r'\bjr\.?\b', r'\bjunior\b', r'\bkid\'?s?\b', r'\bchildren\'?s?\b',
    ]
    for mod in modifiers:
        name = re.sub(mod, '', name)
    # Normalize whitespace and common variations
    name = re.sub(r'\s+', ' ', name).strip()
    name = name.replace('smashburger', 'smash burger')
    name = name.replace('cheeseburger', 'burger')
    name = name.replace('cheese burger', 'burger')
    name = name.replace('french fries', 'fries')
    return name


def upsert_dish(dish: dict):
    """Insert a dish only if a similar one doesn't already exist for this place,
    or if the new source has a higher quality rank (lower number = better).

    Uses normalized dish names for fuzzy dedup.
    """
    client = get_client()
    if not client:
        return

    expert_place_id = dish["expert_place_id"]
    new_normalized = _normalize_dish_name(dish["dish_name"])

    # Get all existing dishes for this place and compare normalized names
    existing_dishes = (
        client.table("place_dishes")
        .select("id, dish_name, source_id, sources(quality_rank)")
        .eq("expert_place_id", expert_place_id)
        .execute()
    )

    match = None
    for existing in (existing_dishes.data or []):
        if _normalize_dish_name(existing["dish_name"]) == new_normalized:
            match = existing
            break

    if match:
        # Only replace if new source has better (lower) quality rank
        old_rank = match.get("sources", {}).get("quality_rank", 999)
        new_rank = _get_source_rank(dish["source_id"])
        if new_rank < old_rank:
            client.table("place_dishes").update({
                "dish_name": dish["dish_name"],
                "source_id": dish["source_id"],
                "review_id": dish["review_id"],
                "sentiment": dish["sentiment"],
                "note": dish.get("note"),
            }).eq("id", match["id"]).execute()
    else:
        client.table("place_dishes").insert(dish).execute()


def _get_source_rank(source_id: str) -> int:
    """Get quality rank for a source. Lower = better."""
    client = get_client()
    if not client:
        return 999
    result = client.table("sources").select("quality_rank").eq("id", source_id).limit(1).execute()
    return result.data[0]["quality_rank"] if result.data else 999


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


def search_dishes_by_keyword(keyword: str) -> set[str]:
    """Return Google Place IDs of places whose expert dishes match a keyword.

    Used by search_my_places to expand keyword search beyond place names/notes
    into the expert dish database.
    """
    db = get_client()
    if not db:
        return set()

    result = (
        db.table("place_dishes")
        .select("expert_place_id, expert_places(google_place_id)")
        .ilike("dish_name", f"%{keyword}%")
        .limit(100)
        .execute()
    )
    return {
        r["expert_places"]["google_place_id"]
        for r in (result.data or [])
        if r.get("expert_places", {}).get("google_place_id")
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
