# Bordbistro Tracker

Daily snapshot + diff of the Deutsche Bahn Bordbistro menu, with a Ghost-embeddable widget and per-item review links.

## Quick start

```bash
pip install -r requirements.txt
python scraper.py --init-db
python scraper.py                  # fetch live, snapshot, diff
python scraper.py --report         # markdown summary of today's changes
BORDBISTRO_ADMIN_TOKEN=secret uvicorn api:app --port 8765
```

Open `http://127.0.0.1:8765/` for the live preview and `/admin` for the admin portal.

## Files

- `scraper.py` — fetch + diff against previous version, writes `db.sqlite` + `snapshots/`, fires the change webhook
- `schema.sql` — SQLite schema (items / item_versions / change_log / reviews / settings)
- `api.py` — FastAPI: read API, stats, settings, image proxy, admin endpoints
- `embed.js` / `embed.css` / `embed.html` — vanilla-JS widget for Ghost (no external/CDN assets)
- `admin.html` — admin portal (overview, changes, reviews, embed settings, webhook) at `/admin`
- `Dockerfile` / `docker-compose.yml` — containerized API + daily scraper sidecar
- `deploy/*.service`, `*.timer` — systemd units (alternative to Docker)

### API endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /api/items` | — | latest menu; `?section=&register=&available=&has_review=&needs_review=&include_hidden=` |
| `GET /api/items/{id}` | — | item + version history + change log |
| `GET /api/items/{id}/availability` | — | availability intervals + total days on menu |
| `GET /api/changes` | — | recent change log; `?since=YYYY-MM-DD&limit=` |
| `GET /api/stats` | — | totals, price min/avg/max, flag counts, new-this-week, insights |
| `GET /api/status` | — | fetch health: `last_success`, `last_attempt`, `last_run` (per-target failures), `latest_snapshot_date` |
| `GET /api/settings` | — | **public** embed config only (heading, intro, lang, accent, …) |
| `GET /api/img/{id}` | — | proxied + on-disk-cached item image (local-first, no third-party hotlink) |
| `GET /api/admin/settings` | ✓ | full config incl. webhook URL/secret |
| `GET /api/admin/fetch-log` | ✓ | raw fetch attempts, newest first; `?limit=` (max 1000) |
| `PUT /api/settings` | ✓ | update any setting (public + admin keys) |
| `POST /api/reviews/{id}` | ✓ | partial upsert: `blog_url`, `rating`, `notes`, `needs_review`, `hidden` |
| `DELETE /api/reviews/{id}` | ✓ | remove review row |
| `POST /api/admin/webhook-test` | ✓ | fire a sample payload at the configured webhook |

## Ghost embed

Paste these two lines into an HTML card on any Ghost post:

```html
<div id="bb-tracker" data-api="https://your-vps"></div>
<script src="https://your-vps/embed.js"></script>
```

The widget renders inside a **Shadow Root**, so the Ghost theme's CSS can't bleed
in (and the widget's CSS can't leak out) — it loads and applies its own styles. No
`<link rel="stylesheet">` is required.

## Admin portal

Open `/admin`, log in with `BORDBISTRO_ADMIN_TOKEN`. Tabs:

- **Übersicht** — stats + insights (cheapest/priciest, train split, change volume).
- **Änderungen** — change log with type badges; jump-to-edit.
- **Abrufe** — fetch log: every scrape attempt per (section, register) with status, item counts, duration and error. The overview shows the last successful fetch + last attempt; the public widget prints the same in its footer (`Stand: …`).
- **Items & Reviews** — set review URL, mark *Needs review*, or **hide** an item from the public embed (manual override for untagged combo offers).
- **Einstellungen** — embed output (heading, intro, default language, show/hide Kombi menus, stats bar, accent colour) + the change webhook.

Or set a review link via API:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"blog_url":"https://blog.example/post","rating":4,"notes":"crispy"}' \
  https://your-vps/api/reviews/24075
```

## Change webhook (n8n / Zapier)

Set a **Webhook-URL** in `/admin` → Einstellungen. After each scrape that finds
changes, the scraper POSTs one JSON payload:

```json
{
  "event": "menu.changed",
  "date": "2026-06-04",
  "count": 3,
  "summary": {"changed": 2, "removed": 1},
  "items": [
    {"id": 24075, "title": "Bratwurstbrötchen …", "section": "ice",
     "register": "speisen", "status": "changed", "image_path": "/api/img/24075",
     "changes": [{"field": "price_eur", "old": "6.9", "new": "7.9"}]}
  ]
}
```

`status` is one of `added | removed | changed | returned`. Set an optional
**Signatur-Secret** and each request carries `X-Bordbistro-Signature: sha256=<hmac>`
(HMAC-SHA256 of the raw body) so your flow can verify authenticity. The URL/secret
live in admin-only settings and are **never** exposed by the public `/api/settings`.

## Deploy (VPS)

```bash
sudo useradd -r -m -d /opt/bordbistro bordbistro
sudo -u bordbistro git clone <this-repo> /opt/bordbistro
cd /opt/bordbistro
sudo -u bordbistro python -m venv .venv && sudo -u bordbistro .venv/bin/pip install -r requirements.txt
sudo -u bordbistro .venv/bin/python scraper.py --init-db
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl enable --now bordbistro-api.service bordbistro-scraper.timer
```

Front with nginx/caddy for TLS; allow CORS for your Ghost domain via `BORDBISTRO_ALLOWED_ORIGIN`.

## Deploy with Docker

```bash
# First time on the VPS
git clone https://github.com/thorbengrosser/bobianalyser.git ~/dockers/bobianalyser
cd ~/dockers/bobianalyser
cp .env.example .env          # set BORDBISTRO_ADMIN_TOKEN + BORDBISTRO_ALLOWED_ORIGIN
docker compose up -d --build  # api on 127.0.0.1:8765 + daily scraper sidecar

# All future updates (one command)
./deploy/deploy.sh
```

`db.sqlite`, `snapshots/`, and `img-cache/` persist in `~/dockers/bobianalyser/data/`. Point your reverse
proxy (Pangolin) at `127.0.0.1:8765`. Schema migrations apply
automatically at startup, so `./deploy/deploy.sh` is a safe upgrade path.

## Search engines

This host is **not indexable**: every response carries `X-Robots-Tag: noindex, nofollow`,
the preview and admin pages add `<meta name="robots" content="noindex, nofollow">`, and
`/robots.txt` deliberately allows crawling so Google can *see* the noindex and drop
already-indexed URLs. The blog post that embeds the widget is unaffected (the widget is
loaded client-side from the blog's own page).

## Notes

- Every scrape writes one `fetch_log` row per target, success or failure. An **empty**
  upstream response while items are known is treated as a failure (logged, snapshot
  skipped) — it would otherwise mark the whole section as removed and fire the webhook.
  `scraper.py` exits non-zero if any target failed.
- The Docker scraper sidecar scrapes once on start, then at every 06:00 UTC.
- Sections are `ice` (ICE long-distance) and `ic2` (IC/EC) — different menus. `items.section` reflects the last-seen section if an item appears in both.
- The embed loads **no external/CDN assets**; item images are proxied + cached through `/api/img/{id}`, and the ingredients view strips the API's inline logo `<img>`s, so a visitor's browser only talks to your server.
- DB tags combo offers with the `kombi` flag — the embed hides them by default (toggle in the toolbar / default in admin settings). Untagged combos can be hidden per-item in `/admin`.
- `ingredients_*` is stored as raw HTML (the API ships it that way); the embed sanitizes to `<b>/<strong>/<br>` only at render time.
- `snapshots/YYYY-MM-DD/<section>-<register>.json.gz` are the source of truth; you can rebuild the DB from them with `--replay` (replay does **not** fire the webhook).
