-- Multi-tenant user data tables
-- Run in Supabase SQL Editor

-- Users
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON users USING (true) WITH CHECK (true);

-- Maps OAuth client_ids to users (a user can have multiple clients)
CREATE TABLE user_clients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_user_clients_client_id ON user_clients(client_id);
ALTER TABLE user_clients ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON user_clients USING (true) WITH CHECK (true);

-- User saved places (per-user, private)
CREATE TABLE user_places (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- Place identity
    name TEXT NOT NULL,
    url TEXT DEFAULT '',
    place_id TEXT DEFAULT '',

    -- User's personal data
    note TEXT DEFAULT '',
    comment TEXT DEFAULT '',
    tags TEXT[] DEFAULT '{}',
    list TEXT DEFAULT 'default',

    -- Google enrichment data
    address TEXT DEFAULT '',
    lat DOUBLE PRECISION,
    lng DOUBLE PRECISION,
    types TEXT[] DEFAULT '{}',
    price_level INTEGER,
    rating REAL,
    phone TEXT DEFAULT '',
    website TEXT DEFAULT '',
    enriched BOOLEAN DEFAULT FALSE,
    business_status TEXT DEFAULT '',

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),

    UNIQUE(user_id, name, url)
);

CREATE INDEX idx_user_places_user_id ON user_places(user_id);
CREATE INDEX idx_user_places_place_id ON user_places(place_id);
CREATE INDEX idx_user_places_user_list ON user_places(user_id, list);
ALTER TABLE user_places ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON user_places USING (true) WITH CHECK (true);

-- User taste profiles (per-user, private)
CREATE TABLE user_taste_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE UNIQUE,
    content TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE user_taste_profiles ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON user_taste_profiles USING (true) WITH CHECK (true);
