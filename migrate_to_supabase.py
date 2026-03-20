"""
One-time migration: move Elliott's local places and taste profile to Supabase.

Usage:
    uv run python migrate_to_supabase.py
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import db

DATA_DIR = Path(os.environ.get("ROUX_DATA_DIR", Path.home() / ".roux"))
PLACES_FILE = DATA_DIR / "places.json"
TASTE_FILE = DATA_DIR / "taste-profile.md"


def migrate():
    client = db.get_client()
    if not client:
        print("No Supabase connection. Check SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.")
        return

    # Create a user for Elliott (or get existing)
    # Use a stable sentinel client_id for the seed user
    SEED_CLIENT_ID = "seed-user-elliott"
    user_id = db.get_or_create_user(SEED_CLIENT_ID)
    print(f"User ID: {user_id}")

    # Migrate places
    if PLACES_FILE.exists():
        with open(PLACES_FILE) as f:
            places = json.load(f)

        print(f"Migrating {len(places)} places...")
        # Batch upsert in chunks to avoid request size limits
        chunk_size = 50
        for i in range(0, len(places), chunk_size):
            chunk = places[i:i + chunk_size]
            for p in chunk:
                p["user_id"] = user_id
                # Ensure all expected fields exist
                p.setdefault("url", "")
                p.setdefault("place_id", "")
                p.setdefault("note", "")
                p.setdefault("comment", "")
                p.setdefault("tags", [])
                p.setdefault("list", "default")
                p.setdefault("address", "")
                p.setdefault("lat", None)
                p.setdefault("lng", None)
                p.setdefault("types", [])
                p.setdefault("price_level", None)
                p.setdefault("rating", None)
                p.setdefault("phone", "")
                p.setdefault("website", "")
                p.setdefault("enriched", False)
                p.setdefault("business_status", "")
                # Remove fields not in schema
                for key in list(p.keys()):
                    if key not in (
                        "user_id", "name", "url", "place_id", "note", "comment",
                        "tags", "list", "address", "lat", "lng", "types",
                        "price_level", "rating", "phone", "website", "enriched",
                        "business_status",
                    ):
                        del p[key]
            db.upsert_user_places(user_id, chunk)
            print(f"  Migrated {min(i + chunk_size, len(places))}/{len(places)}")

        print(f"Done. {len(places)} places migrated.")
    else:
        print(f"No places file at {PLACES_FILE}")

    # Migrate taste profile
    if TASTE_FILE.exists():
        content = TASTE_FILE.read_text()
        db.upsert_user_taste_profile(user_id, content)
        print("Taste profile migrated.")
    else:
        print(f"No taste profile at {TASTE_FILE}")

    print(f"\nMigration complete. User ID: {user_id}")
    print(f"Seed client ID: {SEED_CLIENT_ID}")
    print("Note: When you connect via Claude.ai OAuth, a new client_id will be")
    print("created. You may need to link it to this user in the user_clients table.")


if __name__ == "__main__":
    migrate()
