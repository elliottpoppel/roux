"""Import all Google Takeout CSV files into the Roux places database."""

import asyncio
import sys
from pathlib import Path

# Add the project to the path so we can import server modules
sys.path.insert(0, str(Path(__file__).parent))

from server import parse_takeout_csv, load_places, save_places, enrich_place, ensure_data_dir, GOOGLE_PLACES_API_KEY

TAKEOUT_DIR = Path(__file__).parent / "data" / "Takeout" / "Saved"


async def main():
    ensure_data_dir()
    existing = load_places()
    existing_keys = {(p["name"], p.get("url", "")) for p in existing}

    all_new = []

    for csv_file in sorted(TAKEOUT_DIR.glob("*.csv")):
        list_name = csv_file.stem
        content = csv_file.read_text(encoding="utf-8")
        places = parse_takeout_csv(content)

        added = 0
        for p in places:
            p["list"] = list_name
            if (p["name"], p.get("url", "")) not in existing_keys:
                all_new.append(p)
                existing_keys.add((p["name"], p.get("url", "")))
                added += 1

        if added:
            print(f"  {list_name}: {added} new places")

    if not all_new:
        print("No new places to import.")
        return

    print(f"\nTotal new places: {len(all_new)}")

    # Enrich with Google Places API
    if GOOGLE_PLACES_API_KEY:
        print(f"\nEnriching with Google Places API...")
        enriched = 0
        for i, p in enumerate(all_new):
            if p["name"]:
                all_new[i] = await enrich_place(p)
                if all_new[i].get("enriched"):
                    enriched += 1
                # Progress indicator
                if (i + 1) % 10 == 0:
                    print(f"  {i + 1}/{len(all_new)} processed ({enriched} enriched)")
        print(f"  Enriched {enriched}/{len(all_new)} places")
    else:
        print("\nNo GOOGLE_PLACES_API_KEY set — skipping enrichment.")

    # Save
    existing.extend(all_new)
    save_places(existing)
    print(f"\nDone! Total places in database: {len(existing)}")


if __name__ == "__main__":
    asyncio.run(main())
