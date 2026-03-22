"""
Roux — Re-enrichment script.

Re-resolves all saved places via CID (exact match from Google Maps URL),
cleans up stale expert data for changed place_ids, and re-runs editorial
enrichment from scratch.

Usage:
    uv run python reenrich.py                  # dry run — show what would change
    uv run python reenrich.py --apply          # re-resolve place IDs and update DB
    uv run python reenrich.py --apply --enrich # also re-run editorial enrichment
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))
import db
from server import enrich_place

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("roux.reenrich")

USER_ID = "aeb805ae-e073-4360-b340-345b150d90e2"


async def reenrich_places(dry_run: bool = True, run_editorial: bool = False):
    client = db.get_client()
    if not client:
        logger.error("No Supabase connection")
        return

    places = db.load_user_places(USER_ID)
    logger.info(f"Loaded {len(places)} saved places")

    changed = 0
    unchanged = 0
    errors = 0

    for i, place in enumerate(places):
        name = place.get("name", "")
        old_place_id = place.get("place_id", "")
        url = place.get("url", "")

        # Re-enrich via CID
        enriched = await enrich_place({
            "name": name,
            "url": url,
            "address": place.get("address", ""),
        })

        new_place_id = enriched.get("place_id", "")
        new_address = enriched.get("address", "")

        if not new_place_id:
            logger.warning(f"[{i+1}] {name}: could not resolve")
            errors += 1
            continue

        if new_place_id == old_place_id:
            unchanged += 1
            continue

        changed += 1
        logger.info(
            f"[{i+1}] {name}: CHANGED\n"
            f"       old: {old_place_id} ({place.get('address', '')})\n"
            f"       new: {new_place_id} ({new_address})"
        )

        if not dry_run:
            # Update the user_place record with corrected data
            db.update_user_place(place["id"], {
                "place_id": new_place_id,
                "address": new_address,
                "lat": enriched.get("lat"),
                "lng": enriched.get("lng"),
                "rating": enriched.get("rating"),
                "price_level": enriched.get("price_level"),
                "types": enriched.get("types", []),
                "business_status": enriched.get("business_status", ""),
            })

            # Clean up stale expert data for the old place_id
            old_expert = db.get_expert_place(old_place_id)
            if old_expert:
                expert_id = old_expert["id"]
                # Check if any other user still references this place_id
                other_refs = client.table("user_places").select("id").eq(
                    "place_id", old_place_id
                ).neq("id", place["id"]).limit(1).execute()
                if not other_refs.data:
                    # No other users reference it — safe to delete
                    client.table("place_dishes").delete().eq("expert_place_id", expert_id).execute()
                    client.table("place_reviews").delete().eq("expert_place_id", expert_id).execute()
                    client.table("guide_mentions").delete().eq("expert_place_id", expert_id).execute()
                    client.table("expert_places").delete().eq("id", expert_id).execute()
                    logger.info(f"       Cleaned up stale expert data for old place_id")

        await asyncio.sleep(0.2)  # Rate limit Google API

    logger.info(f"\nResults: {changed} changed, {unchanged} unchanged, {errors} errors")

    if dry_run and changed > 0:
        logger.info("Run with --apply to update the database")
        return

    if not dry_run and run_editorial:
        # Clear last_enriched_at so editorial enrichment re-runs
        logger.info("\nClearing enrichment timestamps for fresh editorial pass...")
        client.table("expert_places").update(
            {"last_enriched_at": None}
        ).neq("id", "00000000-0000-0000-0000-000000000000").execute()

        logger.info("Running editorial enrichment...")
        from enrichment import run_enrichment
        await run_enrichment(force=True, user_id=USER_ID)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Re-enrich all places via CID")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default is dry run)")
    parser.add_argument("--enrich", action="store_true", help="Also re-run editorial enrichment")
    args = parser.parse_args()

    asyncio.run(reenrich_places(dry_run=not args.apply, run_editorial=args.enrich))
