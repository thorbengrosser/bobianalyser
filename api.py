"""FastAPI service: read-only menu API + admin endpoints + embed widget.

Run: uvicorn api:app --host 0.0.0.0 --port 8765
Auth: write endpoints require `Authorization: Bearer <BORDBISTRO_ADMIN_TOKEN>`.
CORS: set BORDBISTRO_ALLOWED_ORIGIN to your Ghost blog origin (default '*').

Privacy / local-first: item images are proxied + cached via /api/img/{id} so a
blog visitor's browser only ever talks to this server, never a third party.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

import requests
from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from scraper import ensure_schema  # single source of truth for migrations

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "db.sqlite"
# Cache beside the (resolved) DB file so it lives in the mounted volume in Docker.
IMG_CACHE = DB_PATH.resolve().parent / "img-cache"
IMG_CACHE_TTL = 7 * 86400  # refetch images at most weekly
UPSTREAM_IMG = "https://db-bordgastronomie.de/files/art-img/?id={id}&type=normal"
UPSTREAM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:151.0) Gecko/20100101 Firefox/151.0",
    "Referer": "https://db-bordgastronomie.de/digitalespeisekarte",
}

ADMIN_TOKEN = os.environ.get("BORDBISTRO_ADMIN_TOKEN", "")
ALLOWED_ORIGIN = os.environ.get("BORDBISTRO_ALLOWED_ORIGIN", "*")

# Public, embed-facing settings (returned by GET /api/settings).
ALLOWED_SETTINGS = {"heading", "intro", "default_lang", "show_kombi", "show_stats", "accent"}
# Admin-only settings — NEVER returned by the public endpoint (may contain secrets
# like an n8n/Zapier webhook URL). Read via GET /api/admin/settings (auth).
ADMIN_SETTINGS = {"webhook_url", "webhook_secret"}
DEFAULT_SETTINGS = {
    "heading": "Bordbistro Menü",
    "intro": "Tagesaktuelle Speisen & Getränke aus ICE & IC/EC.",
    "default_lang": "de",
    "show_kombi": False,
    "show_stats": True,
    "accent": "#ec0016",
}

app = FastAPI(title="Bordbistro Tracker")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if ALLOWED_ORIGIN == "*" else [ALLOWED_ORIGIN],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

# Ensure schema/migrations at startup (handles older DBs missing newer columns).
with sqlite3.connect(DB_PATH) as _c:
    ensure_schema(_c)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def require_admin(authorization: str = Header(default="")) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(500, "admin token not configured")
    expected = f"Bearer {ADMIN_TOKEN}"
    # Constant-time compare to avoid leaking the token via timing.
    if not secrets.compare_digest(authorization, expected):
        raise HTTPException(401, "unauthorized")


def _row_to_item(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("flags_json", "additives_json", "allergens_json"):
        if d.get(k):
            d[k.replace("_json", "")] = json.loads(d[k])
        d.pop(k, None)
    return d


def _clean_url(u) -> str | None:
    """Accept only http(s) URLs — blocks javascript:/data: stored-XSS vectors."""
    if not u:
        return None
    u = str(u).strip()
    return u if u.lower().startswith(("http://", "https://")) else None


def _clean_rating(v) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 5 else None


LATEST_VERSION_SQL = """
SELECT v.* FROM item_versions v
JOIN (
  SELECT item_id, MAX(snapshot_date) AS d
  FROM item_versions GROUP BY item_id
) latest ON latest.item_id = v.item_id AND latest.d = v.snapshot_date
"""


@app.get("/api/items")
def list_items(
    section: str | None = None,
    register: str | None = None,
    available: int = 1,
    has_review: int | None = None,
    needs_review: int | None = None,
    include_hidden: int = 0,
):
    sql = f"""
        SELECT i.id, i.section, i.register, i.first_seen, i.last_seen,
               i.currently_available, v.*,
               r.blog_url, r.rating, r.notes,
               COALESCE(r.needs_review, 0) AS needs_review,
               COALESCE(r.hidden, 0) AS hidden
        FROM items i
        JOIN ({LATEST_VERSION_SQL}) v ON v.item_id = i.id
        LEFT JOIN reviews r ON r.item_id = i.id
        WHERE 1=1
    """
    params: list = []
    if section:
        sql += " AND i.section = ?"; params.append(section)
    if register:
        sql += " AND i.register = ?"; params.append(register)
    if available is not None:
        sql += " AND i.currently_available = ?"; params.append(available)
    if has_review == 1:
        sql += " AND r.blog_url IS NOT NULL"
    elif has_review == 0:
        sql += " AND r.blog_url IS NULL"
    if needs_review == 1:
        sql += " AND COALESCE(r.needs_review, 0) = 1"
    if not include_hidden:
        sql += " AND COALESCE(r.hidden, 0) = 0"
    sql += " ORDER BY v.chapter_id, v.title_de"
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_item(r) for r in rows]


@app.get("/api/items/{item_id}")
def get_item(item_id: int):
    with db() as conn:
        item = conn.execute(
            "SELECT i.*, r.blog_url, r.rating, r.notes, "
            "COALESCE(r.needs_review,0) AS needs_review, COALESCE(r.hidden,0) AS hidden "
            "FROM items i LEFT JOIN reviews r ON r.item_id = i.id WHERE i.id = ?",
            (item_id,),
        ).fetchone()
        if not item:
            raise HTTPException(404, "not found")
        versions = conn.execute(
            "SELECT * FROM item_versions WHERE item_id = ? ORDER BY snapshot_date DESC",
            (item_id,),
        ).fetchall()
        changes = conn.execute(
            "SELECT changed_at, field, old_value, new_value FROM change_log "
            "WHERE item_id = ? ORDER BY changed_at DESC, id DESC",
            (item_id,),
        ).fetchall()
    return {
        "item": dict(item),
        "versions": [_row_to_item(v) for v in versions],
        "changes": [dict(c) for c in changes],
    }


@app.get("/api/items/{item_id}/availability")
def availability(item_id: int):
    """Timeline of availability intervals derived from change_log events."""
    with db() as conn:
        item = conn.execute(
            "SELECT currently_available FROM items WHERE id=?", (item_id,)
        ).fetchone()
        if not item:
            raise HTTPException(404, "not found")
        events = conn.execute(
            "SELECT changed_at, field FROM change_log "
            "WHERE item_id=? AND field IN ('added','removed','returned') "
            "ORDER BY changed_at, id",
            (item_id,),
        ).fetchall()
    intervals: list[dict] = []
    current_start: str | None = None
    for ev in events:
        if ev["field"] in ("added", "returned"):
            if current_start is None:
                current_start = ev["changed_at"]
        elif ev["field"] == "removed":
            if current_start is not None:
                intervals.append({"from": current_start, "to": ev["changed_at"]})
                current_start = None
    if current_start is not None:
        intervals.append({"from": current_start, "to": None})
    total_days = 0
    today = dt.date.today()
    for iv in intervals:
        start = dt.date.fromisoformat(iv["from"])
        end = dt.date.fromisoformat(iv["to"]) if iv["to"] else today
        total_days += (end - start).days + (0 if iv["to"] else 1)
    return {
        "item_id": item_id,
        "currently_available": bool(item["currently_available"]),
        "intervals": intervals,
        "total_days_available": total_days,
    }


@app.get("/api/changes")
def recent_changes(since: str | None = None, limit: int = 200):
    since = since or (dt.date.today() - dt.timedelta(days=30)).isoformat()
    with db() as conn:
        rows = conn.execute(
            """SELECT c.item_id, c.changed_at, c.field, c.old_value, c.new_value,
                      COALESCE(v.title_de, '?') AS title
               FROM change_log c
               LEFT JOIN item_versions v
                 ON v.item_id = c.item_id AND v.snapshot_date = c.changed_at
               WHERE c.changed_at >= ?
               ORDER BY c.changed_at DESC, c.id DESC LIMIT ?""",
            (since, limit),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/stats")
def stats():
    """Headline numbers + insights over the currently-available, non-hidden menu."""
    with db() as conn:
        rows = conn.execute(
            f"""SELECT i.section, i.register, i.first_seen,
                       v.title_de, v.price_eur, v.flags_json,
                       r.blog_url, COALESCE(r.needs_review,0) AS needs_review
                FROM items i
                JOIN ({LATEST_VERSION_SQL}) v ON v.item_id = i.id
                LEFT JOIN reviews r ON r.item_id = i.id
                WHERE i.currently_available = 1 AND COALESCE(r.hidden,0) = 0""",
        ).fetchall()
        today = dt.date.today()
        wk = (today - dt.timedelta(days=7)).isoformat()
        mo = (today - dt.timedelta(days=30)).isoformat()
        changes_7d = conn.execute(
            "SELECT count(*) FROM change_log WHERE changed_at >= ?", (wk,)
        ).fetchone()[0]
        changes_30d = conn.execute(
            "SELECT count(*) FROM change_log WHERE changed_at >= ?", (mo,)
        ).fetchone()[0]

    flag_keys = ["vegan", "vegetarian", "bio", "glutenfree", "lactosefree", "aktion", "neu"]
    flags = {k: 0 for k in flag_keys}
    by_section: dict[str, int] = {}
    by_register: dict[str, int] = {}
    prices: list[float] = []
    with_review = needs_review = new_7d = 0
    cheapest = most_expensive = None
    for r in rows:
        by_section[r["section"]] = by_section.get(r["section"], 0) + 1
        by_register[r["register"]] = by_register.get(r["register"], 0) + 1
        f = json.loads(r["flags_json"] or "{}")
        for k in flag_keys:
            if f.get(k):
                flags[k] += 1
        if r["price_eur"] is not None:
            p = r["price_eur"]
            prices.append(p)
            if cheapest is None or p < cheapest["price"]:
                cheapest = {"title": r["title_de"], "price": p}
            if most_expensive is None or p > most_expensive["price"]:
                most_expensive = {"title": r["title_de"], "price": p}
        if r["blog_url"]:
            with_review += 1
        if r["needs_review"]:
            needs_review += 1
        if r["first_seen"] >= wk:
            new_7d += 1

    return {
        "total": len(rows),
        "by_section": by_section,
        "by_register": by_register,
        "price": {
            "min": round(min(prices), 2) if prices else None,
            "avg": round(sum(prices) / len(prices), 2) if prices else None,
            "max": round(max(prices), 2) if prices else None,
        },
        "flags": flags,
        "with_review": with_review,
        "needs_review": needs_review,
        "new_7d": new_7d,
        "changes_7d": changes_7d,
        "changes_30d": changes_30d,
        "cheapest": cheapest,
        "most_expensive": most_expensive,
    }


@app.get("/api/settings")
def get_settings():
    out = dict(DEFAULT_SETTINGS)
    with db() as conn:
        for row in conn.execute("SELECT key, value FROM settings"):
            if row["key"] in ALLOWED_SETTINGS:
                try:
                    out[row["key"]] = json.loads(row["value"])
                except (TypeError, ValueError):
                    out[row["key"]] = row["value"]
    return out


@app.get("/api/admin/settings", dependencies=[Depends(require_admin)])
def get_admin_settings():
    """Full config including admin-only keys (webhook URL/secret). Auth required."""
    out = dict(DEFAULT_SETTINGS)
    for k in ADMIN_SETTINGS:
        out.setdefault(k, "")
    with db() as conn:
        for row in conn.execute("SELECT key, value FROM settings"):
            if row["key"] in ALLOWED_SETTINGS or row["key"] in ADMIN_SETTINGS:
                try:
                    out[row["key"]] = json.loads(row["value"])
                except (TypeError, ValueError):
                    out[row["key"]] = row["value"]
    return out


@app.put("/api/settings", dependencies=[Depends(require_admin)])
def put_settings(payload: dict):
    items = {k: v for k, v in payload.items() if k in ALLOWED_SETTINGS or k in ADMIN_SETTINGS}
    if not items:
        raise HTTPException(400, "no known settings keys")
    with db() as conn:
        for k, v in items.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, json.dumps(v)),
            )
        conn.commit()
    return get_admin_settings()


@app.post("/api/admin/webhook-test", dependencies=[Depends(require_admin)])
def webhook_test():
    """Fire a sample payload at the configured webhook so n8n/Zapier wiring can be verified."""
    from scraper import get_setting, send_webhook
    with db() as conn:
        url = get_setting(conn, "webhook_url")
        secret = get_setting(conn, "webhook_secret")
    if not url:
        raise HTTPException(400, "no webhook_url configured")
    sample = {
        "event": "menu.changed",
        "date": dt.date.today().isoformat(),
        "count": 1,
        "summary": {"changed": 1},
        "test": True,
        "items": [{
            "id": 24075, "title": "Beispiel-Artikel", "section": "ice", "register": "speisen",
            "status": "changed", "image_path": "/api/img/24075",
            "changes": [{"field": "price_eur", "old": "6.9", "new": "7.9"}],
        }],
    }
    ok, status, detail = send_webhook(url, sample, secret)
    return {"ok": ok, "status": status, "detail": detail}


@app.post("/api/reviews/{item_id}", dependencies=[Depends(require_admin)])
def set_review(item_id: int, payload: dict):
    """Partial update — only keys present in the payload are changed."""
    with db() as conn:
        row = conn.execute(
            "SELECT blog_url, rating, notes, needs_review, hidden FROM reviews WHERE item_id=?",
            (item_id,),
        ).fetchone()
        cur = dict(row) if row else {
            "blog_url": None, "rating": None, "notes": "", "needs_review": 0, "hidden": 0
        }
        if "blog_url" in payload:
            url = payload["blog_url"]
            if url:
                cleaned = _clean_url(url)
                if cleaned is None:
                    raise HTTPException(400, "blog_url must be an http(s) URL")
                cur["blog_url"] = cleaned
            else:
                cur["blog_url"] = None
        if "rating" in payload:
            cur["rating"] = _clean_rating(payload["rating"])
        if "notes" in payload:
            cur["notes"] = str(payload["notes"] or "")
        if "needs_review" in payload:
            cur["needs_review"] = 1 if payload["needs_review"] else 0
        if "hidden" in payload:
            cur["hidden"] = 1 if payload["hidden"] else 0
        conn.execute(
            """INSERT INTO reviews (item_id, blog_url, rating, notes, needs_review, hidden, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(item_id) DO UPDATE SET
                 blog_url=excluded.blog_url, rating=excluded.rating, notes=excluded.notes,
                 needs_review=excluded.needs_review, hidden=excluded.hidden,
                 updated_at=excluded.updated_at""",
            (item_id, cur["blog_url"], cur["rating"], cur["notes"],
             cur["needs_review"], cur["hidden"],
             dt.datetime.utcnow().isoformat(timespec="seconds")),
        )
        conn.commit()
    return {"ok": True, "item_id": item_id, **cur}


@app.delete("/api/reviews/{item_id}", dependencies=[Depends(require_admin)])
def delete_review(item_id: int):
    with db() as conn:
        conn.execute("DELETE FROM reviews WHERE item_id = ?", (item_id,))
        conn.commit()
    return {"ok": True}


@app.get("/api/img/{item_id}")
def item_image(item_id: int):
    """Proxy + on-disk cache for item images (local-first; no third-party hotlink)."""
    IMG_CACHE.mkdir(parents=True, exist_ok=True)
    path = IMG_CACHE / f"{item_id}.jpg"
    fresh = path.exists() and (time.time() - path.stat().st_mtime) < IMG_CACHE_TTL
    if not fresh:
        try:
            r = requests.get(
                UPSTREAM_IMG.format(id=item_id), headers=UPSTREAM_HEADERS, timeout=15
            )
        except requests.RequestException:
            if path.exists():
                return _img_response(path)  # serve stale on upstream failure
            raise HTTPException(502, "upstream image fetch failed")
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
            path.write_bytes(r.content)
        elif not path.exists():
            raise HTTPException(404, "no image")
    return _img_response(path)


def _img_response(path: Path) -> FileResponse:
    return FileResponse(
        path, media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/admin/check", dependencies=[Depends(require_admin)])
def admin_check():
    return {"ok": True}


@app.get("/")
def index():
    # Bare domain shows the live preview widget.
    return FileResponse(ROOT / "embed.html", media_type="text/html")


@app.get("/embed.js")
def embed_js():
    return FileResponse(ROOT / "embed.js", media_type="application/javascript")


@app.get("/embed.css")
def embed_css():
    return FileResponse(ROOT / "embed.css", media_type="text/css")


@app.get("/embed.html")
def embed_html():
    return FileResponse(ROOT / "embed.html", media_type="text/html")


@app.get("/admin")
def admin_page():
    return FileResponse(ROOT / "admin.html", media_type="text/html")
