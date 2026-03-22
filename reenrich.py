"""
Roux — Re-enrichment script.

Re-resolves all saved places via CID (exact match from Google Maps URL),
wipes all enrichment data, and re-runs editorial enrichment from scratch.

Usage:
    uv run python reenrich.py                  # dry run — show what would change
    uv run python reenrich.py --apply          # re-resolve place IDs and update DB
    uv run python reenrich.py --apply --enrich # also wipe + re-run editorial enrichment
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

    # --- Step 1: Re-resolve place IDs via CID ---
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

        await asyncio.sleep(0.2)  # Rate limit Google API

    logger.info(f"\nStep 1 results: {changed} changed, {unchanged} unchanged, {errors} errors")

    if dry_run:
        if changed > 0:
            logger.info("Run with --apply to update the database")
        return

    # --- Step 2: Wipe ALL enrichment data ---
    if run_editorial:
        logger.info("\nStep 2: Wiping all enrichment data for clean re-enrichment...")

        # Get all place IDs that belong to this user's places
        user_places = db.load_user_places(USER_ID)
        place_ids = {p.get("place_id") for p in user_places if p.get("place_id")}

        wiped = 0
        for pid in place_ids:
            expert = db.get_place(pid)
            if expert:
                eid = expert["id"]
                client.table("place_dishes").delete().eq("expert_place_id", eid).execute()
                client.table("place_reviews").delete().eq("expert_place_id", eid).execute()
                client.table("guide_mentions").delete().eq("expert_place_id", eid).execute()
                client.table("places").delete().eq("id", eid).execute()
                wiped += 1

        logger.info(f"Wiped enrichment data for {wiped} places")

        # --- Step 3: Re-run editorial enrichment ---
        logger.info("\nStep 3: Running editorial enrichment from scratch...")
        from enrichment import run_enrichment
        await run_enrichment(force=True, user_id=USER_ID)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Re-enrich all places via CID")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default is dry run)")
    parser.add_argument("--enrich", action="store_true", help="Also wipe + re-run editorial enrichment")
    args = parser.parse_args()

    asyncio.run(reenrich_places(dry_run=not args.apply, run_editorial=args.enrich))
