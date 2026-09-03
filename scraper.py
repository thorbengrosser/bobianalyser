"""Bordbistro menu scraper.

Fetches the DB Bordgastronomie menu API for configured (section, register) pairs,
stores a raw snapshot, upserts the parsed items into SQLite, and records every
field-level diff against the previous version in `change_log`.

Usage:
  python scraper.py                       # fetch live, store, diff
  python scraper.py --replay FILE.json.gz # re-ingest a saved snapshot
  python scraper.py --report              # print markdown summary of today's diffs
  python scraper.py --init-db             # create tables only

Idempotent: running twice in the same day produces zero new change_log rows.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import hmac
import html
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "db.sqlite"
SNAPSHOT_DIR = ROOT / "snapshots"
SCHEMA_PATH = ROOT / "schema.sql"

API_URL = "https://db-bordgastronomie.de/api/api"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:151.0) "
    "Gecko/20100101 Firefox/151.0"
)

# (section, register) pairs to fetch. Probe additional sections (ic, ec, ...) on first
# run; unknown sections log a warning and continue.
TARGETS: list[tuple[str, str]] = [
    ("ice", "speisen"),
    ("ice", "getraenke"),
    ("ic2", "speisen"),
    ("ic2", "getraenke"),
]

# Fields tracked in change_log. Mapped to the column name in item_versions.
TRACKED_SCALAR_FIELDS = [
    "title_de", "title_en", "title_fr",
    "description_de", "description_en", "description_fr",
    "extra_info_de",
    "ingredients_de",
    "chapter_id", "chapter_name",
    "price_eur", "price_chf",
    "price_hint", "price_hint_chf",
    "img_normal",
]
TRACKED_FLAG_FIELDS = [
    "bio", "vegetarian", "vegan", "glutenfree", "lactosefree",
    "aktion", "neu", "neue_rezeptur", "kombi", "limited_stock", "togo", "extra",
]


def build_filter(section: str, register: str) -> dict[str, Any]:
    """Replicates the filter payload the website sends — all defaults included."""
    return {
        "aktion": False, "neu": False, "menu": False,
        "register": register,
        "chapter": None, "initialChapter": None,
        "vegan": False, "vegetarian": False, "bio": False,
        "glutenfree": False, "lactosefree": False,
        "excludeAllergens": [],
        "text": "",
        "favorites": [], "favoritesIc2": [], "showFavorites": False,
        "locale": "de",
        "section": section,
    }


def fetch(section: str, register: str) -> dict[str, Any] | None:
    """Fetch one (section, register). Returns parsed JSON or None on 404/empty."""
    filter_json = json.dumps(build_filter(section, register), separators=(",", ":"))
    url = f"{API_URL}?filter={quote(filter_json)}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "de,en;q=0.9",
        "Referer": "https://db-bordgastronomie.de/digitalespeisekarte",
    }
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    # A valid response always has `artikel`; an empty section returns artikel=[].
    if not isinstance(data, dict) or "artikel" not in data:
        return None
    return data


def save_snapshot(data: dict, section: str, register: str, date: str) -> Path:
    day_dir = SNAPSHOT_DIR / date
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{section}-{register}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    return path


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply schema.sql (idempotent) plus defensive migrations for older DBs.

    `CREATE TABLE IF NOT EXISTS` won't add columns to a pre-existing table, so
    columns introduced after the first release are added here, ignoring the
    'duplicate column' error when they already exist.
    """
    conn.executescript(SCHEMA_PATH.read_text())
    for ddl in (
        "ALTER TABLE reviews ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    return conn


def flatten_article(art: dict) -> dict[str, Any]:
    """Project a raw API article into the column shape we store."""
    flags = {k: bool(art.get(k, False)) for k in TRACKED_FLAG_FIELDS}
    row = {
        "id": int(art["id"]),
        "title_de": art.get("title") or "",
        "title_en": art.get("title_en") or "",
        "title_fr": art.get("title_fr") or "",
        "description_de": art.get("description") or "",
        "description_en": art.get("description_en") or "",
        "description_fr": art.get("description_fr") or "",
        "extra_info_de": art.get("extra_info") or "",
        "ingredients_de": art.get("ingredients") or "",
        "chapter_id": art.get("chapter"),
        "chapter_name": art.get("chapter_name") or "",
        "price_eur": _to_float(art.get("price")),
        "price_chf": _to_float(art.get("price_chf")),
        "price_hint": art.get("price_hint") or "",
        "price_hint_chf": art.get("price_hint_chf") or "",
        "flags": flags,
        "additives": art.get("additives") or [],
        "allergens": art.get("allergens") or [],
        "img_normal": art.get("img_normal") or "",
    }
    return row


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def content_hash(row: dict) -> str:
    """Stable hash over everything we track, for cheap unchanged-row detection."""
    payload = {
        "scalars": {k: row[k] for k in TRACKED_SCALAR_FIELDS if k in row},
        "flags": row["flags"],
        "additives": sorted([a.get("id", "") for a in row["additives"]]),
        "allergens": sorted([a.get("id", "") for a in row["allergens"]]),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def latest_version(conn: sqlite3.Connection, item_id: int) -> dict | None:
    cur = conn.execute(
        "SELECT * FROM item_versions WHERE item_id=? "
        "ORDER BY snapshot_date DESC LIMIT 1",
        (item_id,),
    )
    r = cur.fetchone()
    if r is None:
        return None
    cols = [c[0] for c in cur.description]
    return dict(zip(cols, r))


def diff_against_previous(prev: dict | None, row: dict) -> list[tuple[str, Any, Any]]:
    """Return list of (field, old, new) for fields that differ."""
    diffs: list[tuple[str, Any, Any]] = []
    if prev is None:
        diffs.append(("added", None, row["title_de"]))
        return diffs
    for f in TRACKED_SCALAR_FIELDS:
        old = prev.get(f)
        new = row.get(f)
        # Normalize empty string vs None
        if (old or "") != (new or "") and not (old is None and new is None):
            if isinstance(old, float) or isinstance(new, float):
                if (old or 0) == (new or 0):
                    continue
            diffs.append((f, old, new))
    prev_flags = json.loads(prev.get("flags_json") or "{}")
    for f in TRACKED_FLAG_FIELDS:
        old = bool(prev_flags.get(f, False))
        new = bool(row["flags"].get(f, False))
        if old != new:
            diffs.append((f"flag.{f}", old, new))
    return diffs


def upsert_item(
    conn: sqlite3.Connection,
    row: dict,
    section: str,
    register: str,
    snapshot_date: str,
) -> tuple[list[tuple[str, Any, Any]], bool, bool]:
    """Upsert items + item_versions. Returns (diffs, prev_existed, is_return)."""
    h = content_hash(row)
    prev = latest_version(conn, row["id"])
    prev_existed = prev is not None

    # Detect "returned" — item exists in items table but was marked unavailable.
    state = conn.execute(
        "SELECT currently_available FROM items WHERE id=?", (row["id"],)
    ).fetchone()
    is_return = state is not None and state[0] == 0

    conn.execute(
        """INSERT INTO items (id, section, register, first_seen, last_seen, currently_available)
           VALUES (?, ?, ?, ?, ?, 1)
           ON CONFLICT(id) DO UPDATE SET
             last_seen=excluded.last_seen,
             currently_available=1,
             section=excluded.section,
             register=excluded.register
        """,
        (row["id"], section, register, snapshot_date, snapshot_date),
    )

    if is_return:
        conn.execute(
            "INSERT INTO change_log (item_id, changed_at, field, old_value, new_value) "
            "VALUES (?, ?, 'returned', NULL, ?)",
            (row["id"], snapshot_date, row["title_de"]),
        )

    if prev and prev.get("content_hash") == h:
        # No change since last snapshot — don't write a new version row.
        return [], prev_existed, is_return

    diffs = diff_against_previous(prev, row)

    conn.execute(
        """INSERT OR REPLACE INTO item_versions (
            item_id, snapshot_date,
            title_de, title_en, title_fr,
            description_de, description_en, description_fr,
            extra_info_de, ingredients_de,
            chapter_id, chapter_name,
            price_eur, price_chf, price_hint, price_hint_chf,
            flags_json, additives_json, allergens_json,
            img_normal, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row["id"], snapshot_date,
            row["title_de"], row["title_en"], row["title_fr"],
            row["description_de"], row["description_en"], row["description_fr"],
            row["extra_info_de"], row["ingredients_de"],
            row["chapter_id"], row["chapter_name"],
            row["price_eur"], row["price_chf"], row["price_hint"], row["price_hint_chf"],
            json.dumps(row["flags"], sort_keys=True),
            json.dumps(row["additives"], ensure_ascii=False),
            json.dumps(row["allergens"], ensure_ascii=False),
            row["img_normal"], h,
        ),
    )

    for field, old, new in diffs:
        conn.execute(
            "INSERT INTO change_log (item_id, changed_at, field, old_value, new_value) "
            "VALUES (?, ?, ?, ?, ?)",
            (row["id"], snapshot_date, field, _stringify(old), _stringify(new)),
        )
    return diffs, prev_existed, is_return


def _stringify(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def mark_disappeared(
    conn: sqlite3.Connection,
    section: str,
    register: str,
    present_ids: set[int],
    snapshot_date: str,
) -> list[dict]:
    """Items previously in this (section, register) but missing now → log + flag.

    Returns a change record per removed item (for the webhook payload).
    """
    cur = conn.execute(
        "SELECT id FROM items WHERE section=? AND register=? AND currently_available=1",
        (section, register),
    )
    known = {r[0] for r in cur.fetchall()}
    gone = known - present_ids
    removed: list[dict] = []
    for item_id in gone:
        prev = latest_version(conn, item_id)
        prev_title = (prev or {}).get("title_de", "")
        conn.execute(
            "UPDATE items SET currently_available=0 WHERE id=?", (item_id,)
        )
        conn.execute(
            "INSERT INTO change_log (item_id, changed_at, field, old_value, new_value) "
            "VALUES (?, ?, 'removed', ?, NULL)",
            (item_id, snapshot_date, prev_title),
        )
        removed.append(_change_record(item_id, prev_title, section, register, "removed", []))
    return removed


def _clean_text(s: Any) -> str:
    """Decode HTML entities + strip tags — webhook consumers want plain text."""
    s = html.unescape(str(s or ""))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()


def _change_record(item_id, title, section, register, status, changes) -> dict:
    return {
        "id": item_id,
        "title": _clean_text(title),
        "section": section,
        "register": register,
        "status": status,            # added | removed | changed | returned
        "image_path": f"/api/img/{item_id}",
        "changes": changes,          # [{field, old, new}, ...]
    }


# --------------------------------------------------------------------------- #
# Fetch log — one row per (section, register) attempt, success or failure      #
# --------------------------------------------------------------------------- #

def utc_now() -> str:
    """ISO-8601 UTC timestamp with a trailing Z, second precision."""
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log_fetch(
    conn: sqlite3.Connection,
    run_id: str,
    section: str,
    register: str,
    *,
    ok: bool,
    http_status: int | None = None,
    stats: dict | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
) -> None:
    stats = stats or {}
    conn.execute(
        """INSERT INTO fetch_log (run_id, fetched_at, section, register, ok, http_status,
             present, added, changed, removed, duration_ms, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id, utc_now(), section, register, 1 if ok else 0, http_status,
            stats.get("present"), stats.get("added"), stats.get("changed"), stats.get("removed"),
            duration_ms, (error or None) and str(error)[:300],
        ),
    )
    conn.commit()


def _article_count(data: dict) -> int:
    return sum(
        len(ch.get("articles") or []) for ch in data.get("artikel", []) if isinstance(ch, dict)
    )


# --------------------------------------------------------------------------- #
# Webhook (configured via the `settings` table; admin-only, not in public API) #
# --------------------------------------------------------------------------- #

def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None or row[0] is None:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return row[0]


def send_webhook(url: str, payload: dict, secret: str | None = None) -> tuple[bool, Any, str]:
    """POST payload as JSON. Returns (ok, status_code, detail). Never raises."""
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "bordbistro-tracker/1"}
    if secret:
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Bordbistro-Signature"] = f"sha256={sig}"
    try:
        r = requests.post(url, data=body, headers=headers, timeout=15)
        return (200 <= r.status_code < 300), r.status_code, r.text[:200]
    except requests.RequestException as e:
        return False, None, str(e)


def fire_run_webhook(conn: sqlite3.Connection, date: str, items: list[dict]) -> None:
    """Fire the configured webhook with all changes from a scrape run (if any)."""
    url = get_setting(conn, "webhook_url")
    if not url or not items:
        return
    summary: dict[str, int] = {}
    for it in items:
        summary[it["status"]] = summary.get(it["status"], 0) + 1
    payload = {
        "event": "menu.changed",
        "date": date,
        "count": len(items),
        "summary": summary,
        "items": items,
    }
    ok, status, detail = send_webhook(url, payload, get_setting(conn, "webhook_secret"))
    msg = f"[webhook] {'sent' if ok else 'FAILED'} {len(items)} change(s) → {status or detail}"
    print(msg, file=sys.stdout if ok else sys.stderr)


def ingest(
    conn: sqlite3.Connection,
    data: dict,
    section: str,
    register: str,
    snapshot_date: str,
) -> tuple[dict, list[dict]]:
    """Ingest one parsed API response. Returns (counts, change_records)."""
    present: set[int] = set()
    added = changed = unchanged = 0
    changes: list[dict] = []
    for chapter in data.get("artikel", []):
        for art in chapter.get("articles", []):
            if not isinstance(art, dict) or art.get("id") is None:
                continue  # skip headers / malformed entries
            row = flatten_article(art)
            present.add(row["id"])
            diffs, prev_existed, is_return = upsert_item(
                conn, row, section, register, snapshot_date
            )
            field_changes = [
                {"field": f, "old": _stringify(o), "new": _stringify(n)}
                for f, o, n in diffs if f != "added"
            ]
            if not prev_existed:
                status = "added"; added += 1
            elif is_return:
                status = "returned"; changed += 1
            elif diffs:
                status = "changed"; changed += 1
            else:
                status = None; unchanged += 1
            if status:
                changes.append(_change_record(
                    row["id"], row["title_de"], section, register, status, field_changes
                ))
    removed = mark_disappeared(conn, section, register, present, snapshot_date)
    changes.extend(removed)
    conn.commit()
    stats = {
        "present": len(present),
        "added": added,
        "changed": changed,
        "unchanged": unchanged,
        "removed": len(removed),
    }
    return stats, changes


def run_live(conn: sqlite3.Connection) -> int:
    """Fetch + ingest every target. Returns the number of failed targets.

    Every attempt — success or failure — leaves a row in `fetch_log`, so the API
    can report when the menu was last refreshed and whether the last run worked.
    """
    today = dt.date.today().isoformat()
    run_id = utc_now()
    run_changes: list[dict] = []
    failures = 0
    for section, register in TARGETS:
        t0 = time.monotonic()
        elapsed = lambda: int((time.monotonic() - t0) * 1000)  # noqa: E731

        def fail(msg: str, status: int | None = None) -> None:
            nonlocal failures
            failures += 1
            print(f"[warn] {section}/{register}: {msg}", file=sys.stderr)
            log_fetch(conn, run_id, section, register, ok=False, http_status=status,
                      duration_ms=elapsed(), error=msg)

        try:
            data = fetch(section, register)
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else None
            fail(f"HTTP {code}", code)
            continue
        except requests.RequestException as e:
            fail(f"request failed: {e}")
            continue
        if data is None:
            fail("no data (404 or unexpected payload)")
            continue

        # Guard: an empty `artikel` list while we know items for this target would
        # mark the whole section as removed and fire the webhook. Treat as failure.
        known = conn.execute(
            "SELECT count(*) FROM items WHERE section=? AND register=? AND currently_available=1",
            (section, register),
        ).fetchone()[0]
        if _article_count(data) == 0 and known:
            fail(f"empty response while {known} items are known — skipped to avoid mass removal", 200)
            continue

        save_snapshot(data, section, register, today)
        try:
            stats, changes = ingest(conn, data, section, register, today)
        except Exception as e:  # noqa: BLE001 — log the row, keep the other targets going
            conn.rollback()
            fail(f"ingest failed: {e!r}", 200)
            continue
        run_changes.extend(changes)
        log_fetch(conn, run_id, section, register, ok=True, http_status=200,
                  stats=stats, duration_ms=elapsed())
        print(f"[ok] {section}/{register}: {stats}")
        time.sleep(0.5)  # be polite
    fire_run_webhook(conn, today, run_changes)
    return failures


def run_replay(conn: sqlite3.Connection, path: Path) -> None:
    # Expect filename: <section>-<register>.json.gz, parent dir = date
    snapshot_date = path.parent.name
    name = path.name.replace(".json.gz", "")
    section, register = name.split("-", 1)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    stats, _ = ingest(conn, data, section, register, snapshot_date)
    print(f"[replay] {section}/{register} @ {snapshot_date}: {stats}")


def run_report(conn: sqlite3.Connection, date: str | None = None) -> None:
    date = date or dt.date.today().isoformat()
    cur = conn.execute(
        """SELECT c.item_id, c.field, c.old_value, c.new_value,
                  COALESCE(v.title_de, '?') AS title
           FROM change_log c
           LEFT JOIN item_versions v
             ON v.item_id = c.item_id AND v.snapshot_date = c.changed_at
           WHERE c.changed_at = ?
           ORDER BY c.item_id, c.field""",
        (date,),
    )
    rows = cur.fetchall()
    if not rows:
        print(f"# Bordbistro changes {date}\n\n_No changes._")
        return
    print(f"# Bordbistro changes {date}\n")
    by_item: dict[int, list] = {}
    titles: dict[int, str] = {}
    for item_id, field, old, new, title in rows:
        by_item.setdefault(item_id, []).append((field, old, new))
        titles[item_id] = title
    for item_id, changes in by_item.items():
        print(f"## {titles[item_id]} (id {item_id})")
        for field, old, new in changes:
            if field == "added":
                print("- **added** to menu")
            elif field == "removed":
                print(f"- **removed** from menu (was: {old})")
            elif field == "returned":
                print("- **returned** to menu")
            elif field.startswith("flag."):
                print(f"- {field[5:]}: `{old}` → `{new}`")
            else:
                print(f"- `{field}`: `{old}` → `{new}`")
        print()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--replay", type=Path, help="re-ingest a saved snapshot file")
    p.add_argument("--report", action="store_true", help="markdown summary of today")
    p.add_argument("--report-date", help="YYYY-MM-DD for --report (default today)")
    p.add_argument("--init-db", action="store_true")
    args = p.parse_args()

    conn = init_db()
    if args.init_db:
        print(f"[ok] schema applied → {DB_PATH}")
        return 0
    if args.report:
        run_report(conn, args.report_date)
        return 0
    if args.replay:
        run_replay(conn, args.replay)
        return 0
    # Non-zero exit when any target failed, so systemd/docker logs flag the run.
    return 1 if run_live(conn) else 0


if __name__ == "__main__":
    sys.exit(main())
