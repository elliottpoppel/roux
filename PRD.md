# Roux — Product Requirements Document

## What is Roux?

Roux is a personal dining concierge that makes your Google Maps saved places actually useful. It combines your saves, your notes, and expert editorial knowledge (NYT, Infatuation, Eater, etc.) into a single tool you can query through Claude with natural language.

**Core insight:** People save hundreds of restaurants to Google Maps and never look at them again. Roux turns that dead list into a living, searchable, expert-enriched dining guide.

## Who is it for?

People who save restaurants to Google Maps and eat out frequently. They have opinions, they take notes, and they want better recommendations than "top rated near you."

**V1 user:** Elliott (single-tenant, personal use)
**Future:** Multi-tenant, shareable lists, potentially a subscription product

## The Data Flywheel

Each user who imports their saved places makes Roux better for everyone:

1. **Import** — User imports their Google Maps saved places
2. **Enrich** — Each dining place is enriched with editorial data (dishes, reviews, guides)
3. **Discover** — Guide articles yield additional places not saved by any user (e.g., a "Best Burgers in NYC" guide adds 15 restaurants to the database)
4. **Grow** — As more users import, their saves AND their guide-discovered places compound into a richer shared database
5. **Recommend** — Every user benefits from the full database: their saved places + all discovered places from across the system

More users → more enrichment → bigger expert database → better discovery for everyone.

## Data Model

**One shared database of places.** Every place that any user saves, plus every place discovered from editorial guides, goes into one pool (`places`). This is the knowledge graph.

**Per-user layer on top.** For each user:
- Did they save this place? (`user_places`)
- Did they take notes? (private — `user_places.note`, `user_places.comment`)
- Everything else (dishes, reviews, guides) is shared across all users

**Privacy boundary:** User notes, lists, taste profiles, and locations are private. Expert data (dishes, reviews, guides) is shared globally.

## What does it do?

### Import
- User exports Google Maps saved places via Google Takeout (CSV files)
- Roux imports, deduplicates, and resolves each place via Google Places API (CID-based exact match)
- Non-dining places (museums, parks, etc.) are stored but excluded from all dining features

### Enrich
- For each dining place, Roux searches editorial sources (Brave Search) for reviews and guides
- Fetches articles and extracts structured data via Claude Haiku: dishes, sentiment, summaries
- Guide articles yield data for ALL places mentioned — not just the one being searched for
- If two users save the same restaurant, enrichment happens once (shared expert DB)
- Enrichment runs in the background after import; places are usable immediately via name/note search
- Pipeline versioning enables incremental re-enrichment when the pipeline improves

### Search
- Natural language queries: "best tacos near me," "pizza in Brooklyn," "what should I order at Minetta Tavern"
- Searches across: place names, user notes, Google place types, AND expert dish data
- Results come in two tiers:
  1. **Saved places** — the user's own saves, with their private notes surfaced
  2. **Discoveries** — places from the shared expert database that the user hasn't saved, matched by query and location
- Discovery comes exclusively from Roux's own database — never from Google API. The value is in curated, editorially-backed places, not generic nearby results.
- Google Places API is used only for geocoding ("near Times Square" → coordinates) and live details (hours, open now).

### Recommend
- Every recommendation names specific dishes to order
- User notes are first-class — interweaved with expert data, not quoted verbatim
- Saved places always surface before discoveries
- Results are pre-curated server-side (capped, ranked, query-relevant dishes only) — Claude presents, Roux curates

### Learn
- Locations (home, work, etc.) saved for "near me" queries
- Taste profile exists for future personalization (out of scope for now)

## What it does NOT do

- Cooking, recipes, wine/food knowledge, nutrition, grocery shopping
- Any food question that isn't about a dining establishment
- Replace Google Maps — Roux reads from it, doesn't write to it
- Real-time reservations or ordering
- Use Google API for discovery — discovery is Roux's own database only

## Architecture

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Server | FastMCP (Python) | MCP protocol, 6 tools |
| Database | Supabase (Postgres) | User data (private) + shared expert knowledge |
| Hosting | Render | HTTP transport for Claude.ai |
| Auth | Custom OAuth 2.1 | Password-gated, Claude.ai compatible |
| Editorial Search | Brave Search API | Find reviews and guides for enrichment |
| Places | Google Places API | Import resolution, geocoding, live details only |
| Extraction | Claude Haiku | Structured data from articles |

## Tools

| Tool | Purpose |
|------|---------|
| `search_places` | Primary tool. Search saved places + discover from expert DB. |
| `place_details` | Deep dive on one place: hours, dishes, reviews. Auto-enriches on demand. |
| `import_places` | Import Google Takeout CSV. Diffs against existing. Triggers background enrichment. |
| `taste_profile` | Read/write dining preferences. |
| `locations` | Save labeled locations (home, work, etc.) |
| `my_stats` | Database summary: counts, categories, cities, enrichment progress. |

## Editorial Sources

10 approved domains: NYT, Michelin Guide, The Infatuation, Grub Street, Eater, Bon Appétit, Tasting Table, Serious Eats, Time Out, Thrillist

## Quality Bar

- A "best burger" query in NYC should surface Corner Bistro, Minetta Tavern, JG Melon — the places a knowledgeable local would name
- Recommendations should include specific dishes, not just place names
- Non-dining saves should never appear in results
- Guides expand the knowledge graph: one "Best Burgers" article should add 15 restaurants to the database, not just enrich the one we were searching for

## Open Questions

- What's the onboarding flow for a new user with zero saved places?
- When does this become multi-user? What changes technically?
