CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  section TEXT NOT NULL,
  register TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  currently_available INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS item_versions (
  item_id INTEGER NOT NULL,
  snapshot_date TEXT NOT NULL,
  title_de TEXT, title_en TEXT, title_fr TEXT,
  description_de TEXT, description_en TEXT, description_fr TEXT,
  extra_info_de TEXT,
  ingredients_de TEXT,
  chapter_id INTEGER, chapter_name TEXT,
  price_eur REAL, price_chf REAL,
  price_hint TEXT, price_hint_chf TEXT,
  flags_json TEXT,
  additives_json TEXT,
  allergens_json TEXT,
  img_normal TEXT,
  content_hash TEXT NOT NULL,
  PRIMARY KEY (item_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS change_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL,
  changed_at TEXT NOT NULL,
  field TEXT NOT NULL,
  old_value TEXT,
  new_value TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
  item_id INTEGER PRIMARY KEY,
  blog_url TEXT,
  rating INTEGER,
  notes TEXT,
  needs_review INTEGER NOT NULL DEFAULT 0,
  hidden INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

-- Embed/admin configuration as key/value JSON. Single-row-per-key.
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

-- One row per (section, register) fetch attempt. `ok=1` means the upstream API
-- answered and the response was ingested; `error` explains failures.
CREATE TABLE IF NOT EXISTS fetch_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,          -- ISO timestamp (UTC) shared by all rows of one scrape run
  fetched_at TEXT NOT NULL,      -- ISO timestamp (UTC) of this attempt
  section TEXT NOT NULL,
  register TEXT NOT NULL,
  ok INTEGER NOT NULL,
  http_status INTEGER,
  present INTEGER, added INTEGER, changed INTEGER, removed INTEGER,
  duration_ms INTEGER,
  error TEXT
);

CREATE INDEX IF NOT EXISTS idx_fetch_log_time ON fetch_log(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_changes_item ON change_log(item_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_changes_recent ON change_log(changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_versions_date ON item_versions(snapshot_date);
