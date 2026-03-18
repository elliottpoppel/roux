# Roux

Your personal dining concierge. Roux makes your Google Maps saved places actually useful by letting you query them through Claude with natural language.

**"What's the best pizza place near me that's open right now?"** — Roux searches your saved places, checks what's open, and gives you a recommendation.

## What It Does

- **Search your saved places** by cuisine, location, vibe, or your own notes
- **Get real-time details** — hours, whether it's open now, ratings, reviews
- **Discover new places** that match your taste, beyond your saved list
- **Location-aware** — "near Times Square" or "in Shibuya" just works
- **Works through Claude** — no app to install, just talk naturally

## Setup

### 1. Install uv (Python package manager)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Clone this repo

```bash
git clone https://github.com/yourname/roux.git
cd roux
uv sync
```

### 3. Get a Google Places API Key (recommended)

Without this, Roux works with your saved places data only (names, notes, URLs). With it, you get real-time hours, ratings, reviews, location search, and discovery.

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project (or use an existing one)
3. Enable the **Places API** and **Geocoding API**
4. Create an API key under Credentials
5. The free tier gives you $200/month of credit — more than enough for personal use

### 4. Connect to Claude

Add Roux to your Claude Code project. Create or edit `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "roux": {
      "command": "uv",
      "args": ["--directory", "/path/to/roux", "run", "server.py"],
      "env": {
        "GOOGLE_PLACES_API_KEY": "your-api-key-here",
        "ROUX_DEFAULT_LOCATION": "New York, NY"
      }
    }
  }
}
```

Replace `/path/to/roux` with the actual path where you cloned the repo.

### 5. Import Your Google Maps Saved Places

This is a one-time setup (re-do it whenever you want to sync new saves):

1. **Open this link:** [Google Takeout — Saved Places Only](https://takeout.google.com/settings/takeout/custom/maps)
2. **Make sure only "Saved" is selected** (deselect everything else if needed)
3. **Click "Next step"**, then **"Create export"**
4. **Wait for the email** (usually a few minutes), then **download the zip**
5. **Unzip it** — inside you'll find a `Saved` folder with CSV files (one per list)
6. **Tell Claude:** "Import my saved places" and paste the contents of the CSV file

That's it. Your places are now searchable.

## Usage Examples

Once set up, just talk to Claude naturally:

- "What pizza places have I saved near the West Village?"
- "I'm going to a show near Times Square Friday night. Where should I eat?"
- "Show me all my saved coffee shops"
- "What's open near me right now for a quick bite?"
- "Find me a good ramen place in Shibuya — check my saves first"
- "What are my stats? How many places have I saved?"

## Tools Available

| Tool | What It Does |
|------|-------------|
| `import_places` | Import a Google Takeout CSV file |
| `search_my_places` | Search your saved places by query, cuisine, location |
| `get_place_info` | Get real-time details for any place (hours, reviews, etc.) |
| `discover_places` | Find new places near a location via Google Places |
| `add_note` | Add or update a note on a saved place |
| `my_places_stats` | See a summary of your saved places database |

## Configuration

Environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `GOOGLE_PLACES_API_KEY` | Recommended | Enables real-time hours, ratings, discovery, and location search |
| `ROUX_DEFAULT_LOCATION` | Optional | Your home base (e.g. "New York, NY") so "near me" works without asking |
| `ROUX_DATA_DIR` | Optional | Where to store the places database (default: `~/.roux`) |

## How It Works

1. You export your Google Maps saved places via Google Takeout (CSV files)
2. Roux imports and stores them locally in `~/.roux/places.json`
3. If you have a Google Places API key, it enriches each place with coordinates, cuisine type, ratings, and more
4. When you ask Claude a question, it uses Roux's tools to search your places, check real-time details, or discover new spots
5. Claude combines this data with its own knowledge to give you useful recommendations

## Refreshing Your Data

Saved new places in Google Maps? Just re-export from Google Takeout and import again. Roux will only add new places — it won't duplicate existing ones.
