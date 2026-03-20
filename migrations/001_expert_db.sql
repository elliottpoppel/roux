-- Expert knowledge base (shared across all users)
-- Run in Supabase SQL Editor

CREATE TABLE sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('city', 'national', 'global')),
    city TEXT,
    type TEXT NOT NULL CHECK (type IN ('institutional', 'editorial', 'substack', 'social')),
    quality_rank INTEGER NOT NULL,
    crawl_strategy TEXT NOT NULL CHECK (crawl_strategy IN ('full', 'free_tier', 'headlines_only', 'passive')),
    approved BOOLEAN DEFAULT FALSE,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE sources ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON sources USING (true) WITH CHECK (true);

CREATE TABLE expert_places (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    google_place_id TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    address TEXT,
    city TEXT,
    country TEXT,
    lat DOUBLE PRECISION,
    lng DOUBLE PRECISION,
    place_types TEXT[],
    price_level INTEGER,
    google_rating DOUBLE PRECISION,
    website TEXT,
    phone TEXT,
    last_enriched_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE expert_places ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON expert_places USING (true) WITH CHECK (true);

CREATE TABLE place_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    expert_place_id UUID REFERENCES expert_places(id) ON DELETE CASCADE,
    source_id UUID REFERENCES sources(id) ON DELETE CASCADE,
    url TEXT,
    title TEXT,
    published_at DATE,
    sentiment TEXT CHECK (sentiment IN ('positive', 'mixed', 'negative')),
    summary TEXT,
    raw_text TEXT,
    fetched_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(expert_place_id, source_id, url)
);

ALTER TABLE place_reviews ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON place_reviews USING (true) WITH CHECK (true);

CREATE TABLE place_dishes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    expert_place_id UUID REFERENCES expert_places(id) ON DELETE CASCADE,
    source_id UUID REFERENCES sources(id) ON DELETE CASCADE,
    review_id UUID REFERENCES place_reviews(id) ON DELETE CASCADE,
    dish_name TEXT NOT NULL,
    sentiment TEXT NOT NULL CHECK (sentiment IN ('must_order', 'recommended', 'skip', 'overhyped', 'mixed')),
    note TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE place_dishes ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON place_dishes USING (true) WITH CHECK (true);

CREATE TABLE guides (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID REFERENCES sources(id) ON DELETE CASCADE,
    url TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    theme TEXT,
    city TEXT,
    scope TEXT CHECK (scope IN ('city', 'national', 'global')),
    published_at DATE,
    fetched_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE guides ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON guides USING (true) WITH CHECK (true);

CREATE TABLE guide_mentions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    guide_id UUID REFERENCES guides(id) ON DELETE CASCADE,
    expert_place_id UUID REFERENCES expert_places(id) ON DELETE CASCADE,
    context TEXT,
    rank INTEGER,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(guide_id, expert_place_id)
);

ALTER TABLE guide_mentions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON guide_mentions USING (true) WITH CHECK (true);

-- Indexes
CREATE INDEX ON expert_places(city);
CREATE INDEX ON expert_places(google_place_id);
CREATE INDEX ON place_dishes(expert_place_id);
CREATE INDEX ON place_dishes(dish_name);
CREATE INDEX ON guide_mentions(expert_place_id);
CREATE INDEX ON place_reviews(expert_place_id);
CREATE INDEX ON place_dishes USING gin(to_tsvector('english', dish_name));
CREATE INDEX ON place_reviews USING gin(to_tsvector('english', coalesce(summary, '') || ' ' || coalesce(raw_text, '')));
