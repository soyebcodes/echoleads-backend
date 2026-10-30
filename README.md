# EchoLeads Backend

FastAPI scanner service for EchoLeads. It fetches posts from **Reddit, Hacker News, and X (Twitter)**, filters them by your campaign keywords, scores buying intent with **Ollama Cloud** (Groq automatic fallback), and writes matched leads into the shared Postgres/Supabase database.

The Next.js app triggers scans with `POST /run`. The frontend also has a built-in TypeScript scanner — this service is optional and is used whenever the frontend has `PYTHON_API_URL` configured.

## What it does

- Health/status endpoints for local and production checks
- Accepts scan triggers via `POST /run` (single campaign or up to 5 recent campaigns)
- Loads campaign context, keywords, plan, and remaining quota from the database
- Enforces the same plan limits as the frontend (platforms + monthly lead quota)
- Fetches from every selected platform in parallel and applies negative/positive keyword filters
- AI-scores candidates (Ollama Cloud → Groq → keyword heuristic fallback) with a per-lead match reason
- Inserts leads above score 70, deduped per campaign/platform, capped at the remaining quota
- Updates campaign status (running → success / failed + message)

## Schema compatibility

The scanner works against **both** schema versions:

| Schema | Leads columns used |
|--------|--------------------|
| v0.2 (current) | `platform`, `external_id`, `ai_score_reason`, unique `(campaign_id, platform, external_id)` |
| v0.1 (legacy) | `reddit_post_id`, unique `(reddit_post_id)` |

Columns are detected at runtime via `information_schema`, so you can deploy this service before or after applying `supabase/migration_v02_multi_platform_billing.sql` in the frontend repo with zero downtime. X and Hacker News leads require the v0.2 schema.

## Local setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run the API

```powershell
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

## Health check

```powershell
curl http://localhost:8000/health
```

Returns the configured AI providers and whether X scraping is configured.

## Trigger a scan

```powershell
curl -X POST http://localhost:8000/run -H "Content-Type: application/json" -d "{\"campaign_id\":\"<campaign-id>\"}"
```

Omit `campaign_id` (or send `null`) to scan the 5 most recent campaigns. If `SCANNER_API_KEY` is set, send it as the `x-api-key` header.

## Environment variables

Set these in `.env` (see `.env.example`):

| Variable | Required | Purpose |
|----------|----------|---------|
| `DATABASE_URL` | ✅ | Postgres connection string for the shared EchoLeads database |
| `OLLAMA_API_KEY` | Recommended | Ollama Cloud AI scoring (primary) |
| `OLLAMA_BASE_URL` | No | Defaults to `https://ollama.com/v1` |
| `OLLAMA_MODEL` | No | Defaults to `deepseek-v4.1-flash` |
| `GROQ_API_KEY` | No | Groq fallback |
| `GROQ_MODEL` | No | Defaults to `openai/gpt-oss-20b` |
| `X_BEARER_TOKEN` | No | X API v2 recent search |
| `APIFY_API_TOKEN` | No | Apify tweet scraper alternative |
| `APIFY_X_ACTOR_ID` | No | Defaults to `apido~tweet-scraper` |
| `SCANNER_API_KEY` | No | Shared secret; when set, `POST /run` requires `x-api-key` |

Without any AI key the service still works using keyword heuristic scoring.

## Tests

```powershell
pip install -r requirements.txt
python -m unittest -v
```

## Deployment

### Render (free tier)

1. Create a new **Web Service** and connect this repo
2. **Runtime:** Python 3
3. **Build Command:** `pip install -r requirements.txt`
4. **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. **Environment Variables:** `DATABASE_URL`, `OLLAMA_API_KEY`, and optionally the rest
6. **Instance Type:** Free

### Other options

Fly.io, Koyeb, and Railway all support the same `uvicorn main:app` start command.

## Connecting the frontend

Set these on the EchoLeads frontend (Next.js):

```
PYTHON_API_URL=https://<your-backend-url>
SCANNER_API_KEY=<same value as on this service, optional>
```

When `PYTHON_API_URL` is set, the frontend delegates every scan to this service. When it is not set, the frontend uses its built-in TypeScript scanner instead — both enforce the same plan quotas and dedupe rules.

## API reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Root status check |
| `GET` | `/health` | Health check + AI/X configuration |
| `POST` | `/run` | Trigger a scan (body: `{"campaign_id": "<uuid>"}`, optional `x-api-key`) |

## License

ISC © [Soyeb Islam](https://github.com/soyebcodes)