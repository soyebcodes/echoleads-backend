# EchoLeads Backend

FastAPI service that powers Reddit lead scanning for EchoLeads. It fetches Reddit RSS data, filters and scores candidate posts, and writes matched leads into the shared Postgres/Supabase database.

The frontend (Next.js app) triggers scans by sending `POST /run` to this service.

## What it does

- Exposes health endpoints for local and production checks
- Accepts scan triggers via `POST /run`
- Loads campaign context from the database
- Searches Reddit, scores the content, and inserts new leads when a match is strong enough
- Updates campaign status (running → success / failed) after each scan

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

## Trigger a scan

```powershell
curl -X POST http://localhost:8000/run -H "Content-Type: application/json" -d "{\"campaign_id\":\"<campaign-id>\"}"
```

Omit `campaign_id` (or send `null`) to scan all campaigns.

## Environment variables

Set these in `.env` (see `.env.example`):

- `DATABASE_URL` — Postgres connection string for the shared EchoLeads database

## Deployment

### Render (free tier)

1. Create a new **Web Service** and connect this repo
2. **Runtime:** Python 3
3. **Build Command:** `pip install -r requirements.txt`
4. **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. **Environment Variable:** `DATABASE_URL`
6. **Instance Type:** Free

### Other options

Fly.io, Koyeb, and Railway all support the same `uvicorn main:app` start command — just set `DATABASE_URL`.

## Connecting the frontend

Set this on the EchoLeads frontend (Next.js) to point scans at this backend:

```
PYTHON_API_URL=https://<your-backend-url>
```

## API reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Root status check |
| `GET` | `/health` | Health check |
| `POST` | `/run` | Trigger a scan (body: `{"campaign_id": "<uuid>"}`) |

## License

ISC © [Soyeb Islam](https://github.com/soyebcodes)
