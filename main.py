import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import psycopg2
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv(Path(__file__).resolve().parent / ".env")

app = FastAPI(title="EchoLeads Scanner API", version="2.0.0")

DATABASE_URL = os.getenv("DATABASE_URL")
SCANNER_API_KEY = os.getenv("SCANNER_API_KEY")

OLLAMA_BASE_URL = (os.getenv("OLLAMA_BASE_URL") or "https://ollama.com/v1").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL") or "deepseek-v4.1-flash"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = os.getenv("GROQ_MODEL") or "openai/gpt-oss-20b"

X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN")
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN")
APIFY_X_ACTOR_ID = os.getenv("APIFY_X_ACTOR_ID") or "apido~tweet-scraper"

SCORE_THRESHOLD = 70
MAX_CANDIDATES = 24
MAX_RESULTS_PER_PLATFORM = 25
AI_WORKERS = 6
FETCH_WORKERS = 3

PLATFORM_LABELS = {
    "reddit": "Reddit",
    "hacker_news": "Hacker News",
    "x": "X (Twitter)",
}

PLAN_LIMITS = {
    "free": {"campaigns": 1, "leads_per_month": 50, "platforms": ["reddit", "hacker_news"]},
    "pro": {"campaigns": 5, "leads_per_month": 1000, "platforms": ["reddit", "hacker_news", "x"]},
    "business": {"campaigns": 25, "leads_per_month": 5000, "platforms": ["reddit", "hacker_news", "x"]},
}

BILLABLE_STATUSES = {"active", "trialing", "past_due"}

REDDIT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36 EchoLeads/2.0"
)


class RunRequest(BaseModel):
    campaign_id: Optional[str] = None


def table_has_column(cur: Any, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return cur.fetchone() is not None


def mark_campaign_run(
    cur: Any,
    campaign_id: Optional[str],
    status: str,
    error: Optional[str] = None,
) -> None:
    if campaign_id is None:
        return
    try:
        cur.execute(
            """
            UPDATE campaigns
            SET last_run_at = NOW(),
                last_run_status = %s,
                last_run_error = %s
            WHERE id = %s
            """,
            (status, error, campaign_id),
        )
    except Exception as exc:
        print(f"Could not update run status for campaign {campaign_id}: {exc}")


def clean_text(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def strip_html(value: Optional[str]) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]*>", " ", value)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&apos;", "'")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&#32;", " ")
    return clean_text(text)


def parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def get_user_plan(cur: Any, user_id: Optional[str]) -> str:
    if not user_id:
        return "free"
    try:
        cur.execute(
            "SELECT plan, status FROM subscriptions WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return "free"
        plan, status = row
        if status not in BILLABLE_STATUSES:
            return "free"
        return plan if plan in PLAN_LIMITS else "free"
    except Exception:
        return "free"


def get_remaining_lead_quota(cur: Any, user_id: Optional[str], plan: str) -> int:
    if not user_id:
        return PLAN_LIMITS["free"]["leads_per_month"]
    limit = PLAN_LIMITS[plan]["leads_per_month"]
    cur.execute(
        """
        SELECT count(*)
        FROM leads l
        JOIN campaigns c ON l.campaign_id = c.id
        WHERE c.user_id = %s AND l.created_at >= date_trunc('month', NOW())
        """,
        (user_id,),
    )
    used = cur.fetchone()[0] or 0
    return max(0, limit - used)


def build_search_query(pos_keywords: list[str], neg_keywords: list[str], fallback_terms: list[str]) -> str:
    terms = [kw for kw in pos_keywords if kw.strip()][:6]
    if terms:
        query = "(" + " OR ".join(f'"{kw}"' for kw in terms) + ")"
    else:
        seeds = [t for t in fallback_terms if len(t) > 2][:6]
        query = "(" + " OR ".join(f'"{t}"' for t in seeds) + ")" if seeds else "saas"

    for neg in neg_keywords[:3]:
        query += f' -"{neg}"'
    return query


def derived_terms(name: str, description: Optional[str], target_description: Optional[str], lead_type: Optional[str]) -> list[str]:
    seed_terms: list[str] = []
    for value in [name, description, target_description]:
        if value:
            seed_terms.extend(re.split(r"[^a-z0-9]+", value.lower()))
    extra = (
        ["freelancer", "developer", "hire", "saas", "startup"]
        if lead_type == "service"
        else ["saas", "software", "tool", "startup"]
    )
    stop = {"the", "and", "for", "with", "that", "this", "your", "help", "looking"}
    return [t for t in dict.fromkeys(seed_terms + extra) if len(t) > 2 and t not in stop]


def recency_window(days: int) -> str:
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 30:
        return "month"
    return "year"


def fetch_reddit(payload: dict) -> list[dict]:
    query = build_search_query(payload["pos_keywords"], payload["neg_keywords"], payload["fallback_terms"])
    url = (
        "https://www.reddit.com/search.rss"
        f"?q={requests.utils.quote(query)}&sort=new&t={recency_window(payload['time_filter_days'])}&limit=100"
    )

    response = None
    for attempt in range(1, 4):
        try:
            response = requests.get(
                url,
                timeout=20,
                headers={"User-Agent": REDDIT_USER_AGENT},
            )
            response.raise_for_status()
            break
        except requests.exceptions.HTTPError as exc:
            if response is not None and response.status_code == 429 and attempt < 3:
                wait_secs = 10 * attempt
                print(f"[Scan] Reddit 429, retry {attempt}/3 in {wait_secs}s")
                time.sleep(wait_secs)
                continue
            raise RuntimeError(f"Reddit fetch failed: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"Reddit fetch failed: {exc}") from exc

    if response is None:
        return []

    soup = BeautifulSoup(response.text, "xml")
    entries = soup.find_all("entry")
    cutoff = datetime.now(timezone.utc).timestamp() - payload["time_filter_days"] * 86400

    posts = []
    for entry in entries:
        published = entry.published.get_text() if entry.published else ""
        created = parse_iso_timestamp(published)
        if created and created.timestamp() < cutoff:
            continue

        title = strip_html(entry.title.get_text() if entry.title else "")
        content = strip_html(entry.content.get_text() if entry.content else "")
        author_node = entry.author.find("name") if entry.author else None
        author = (author_node.get_text() if author_node else "").replace("u/", "").strip() or "unknown"
        link = entry.link.get("href", "") if entry.link else ""
        post_id = entry.id.get_text() if entry.id else ""

        if not post_id or not link or (not title and not content):
            continue

        posts.append(
            {
                "platform": "reddit",
                "external_id": post_id,
                "title": title or "(untitled post)",
                "content": content or None,
                "url": link,
                "author": author,
                "created_at": published or None,
                "likes": None,
                "comments": None,
            }
        )

    return posts[: payload["max_results"]]


def fetch_hackernews(payload: dict) -> list[dict]:
    query = build_search_query(payload["pos_keywords"], payload["neg_keywords"], payload["fallback_terms"])
    since = int(time.time() - payload["time_filter_days"] * 86400)
    filters = [f"created_at_i>{since}"]
    if payload["min_likes"] > 0:
        filters.append(f"points>={payload['min_likes']}")
    if payload["min_comments"] > 0:
        filters.append(f"num_comments>={payload['min_comments']}")

    url = (
        "https://hn.algolia.com/api/v1/search_by_date"
        f"?query={requests.utils.quote(query)}&tags=(story,comment)"
        f"&numericFilters={requests.utils.quote(','.join(filters))}"
        f"&hitsPerPage={min(50, payload['max_results'] * 2)}"
    )
    response = requests.get(url, timeout=15, headers={"Accept": "application/json"})
    response.raise_for_status()
    hits = response.json().get("hits", [])

    posts = []
    for hit in hits:
        object_id = hit.get("objectID")
        title = strip_html(hit.get("title") or hit.get("story_title") or "")
        content = strip_html(hit.get("comment_text") or hit.get("story_text") or "")
        if not object_id or (not title and not content):
            continue
        posts.append(
            {
                "platform": "hacker_news",
                "external_id": object_id,
                "title": title or content[:100],
                "content": content or None,
                "url": f"https://news.ycombinator.com/item?id={object_id}",
                "author": hit.get("author") or "unknown",
                "created_at": hit.get("created_at"),
                "likes": hit.get("points"),
                "comments": hit.get("num_comments"),
            }
        )

    return posts[: payload["max_results"]]


def is_x_configured() -> bool:
    return bool(X_BEARER_TOKEN or APIFY_API_TOKEN)


def fetch_x_official(payload: dict) -> list[dict]:
    query = " OR ".join(f'"{kw}" -is:retweet' for kw in payload["pos_keywords"][:6])
    response = requests.get(
        "https://api.x.com/2/tweets/search/recent",
        params={
            "query": query,
            "max_results": min(25, payload["max_results"] * 2),
            "sort_order": "recency",
            "tweet.fields": "created_at,public_metrics,author_id",
            "expansions": "author_id",
            "user.fields": "username",
        },
        headers={"Authorization": f"Bearer {X_BEARER_TOKEN}", "User-Agent": "EchoLeads/2.0"},
        timeout=20,
    )
    if not response.ok:
        detail = ""
        try:
            detail = response.json().get("title") or response.json().get("detail") or ""
        except Exception:
            detail = response.text[:200]
        raise RuntimeError(f"X API search failed ({response.status_code}): {detail}")

    data = response.json()
    usernames = {
        user.get("id"): user.get("username")
        for user in (data.get("includes", {}) or {}).get("users", [])
    }
    cutoff = datetime.now(timezone.utc).timestamp() - payload["time_filter_days"] * 86400

    posts = []
    for tweet in data.get("data", []) or []:
        metrics = tweet.get("public_metrics") or {}
        created = parse_iso_timestamp(tweet.get("created_at"))
        if created and created.timestamp() < cutoff:
            continue
        if (metrics.get("like_count") or 0) < payload["min_likes"]:
            continue
        if (metrics.get("reply_count") or 0) < payload["min_comments"]:
            continue

        username = usernames.get(tweet.get("author_id")) or "unknown"
        text = clean_text(tweet.get("text"))
        if not text:
            continue
        posts.append(
            {
                "platform": "x",
                "external_id": tweet.get("id"),
                "title": text[:120],
                "content": text,
                "url": f"https://x.com/{username}/status/{tweet.get('id')}",
                "author": username,
                "created_at": tweet.get("created_at"),
                "likes": metrics.get("like_count"),
                "comments": metrics.get("reply_count"),
            }
        )

    return posts[: payload["max_results"]]


def fetch_x_apify(payload: dict) -> list[dict]:
    since = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    response = requests.post(
        f"https://api.apify.com/v2/acts/{APIFY_X_ACTOR_ID}/run-sync-get-dataset-items",
        params={"token": APIFY_API_TOKEN},
        json={
            "searchTerms": payload["pos_keywords"][:6],
            "sort": "Latest",
            "maxItems": min(50, payload["max_results"] * 2),
            "since": since,
        },
        timeout=90,
    )
    if not response.ok:
        raise RuntimeError(f"Apify X scraper failed ({response.status_code}): {response.text[:200]}")

    cutoff = datetime.now(timezone.utc).timestamp() - payload["time_filter_days"] * 86400
    posts = []
    for tweet in response.json() or []:
        tweet_id = tweet.get("id")
        text = clean_text(tweet.get("text"))
        if not tweet_id or not text:
            continue
        created = parse_iso_timestamp(tweet.get("createdAt"))
        if created and created.timestamp() < cutoff:
            continue
        likes = tweet.get("likeCount") or 0
        replies = tweet.get("replyCount") or 0
        if (likes > 0 or replies > 0) and (
            likes < payload["min_likes"] or replies < payload["min_comments"]
        ):
            continue
        author = (
            (tweet.get("author") or {}).get("userName")
            or (tweet.get("author_meta") or {}).get("userName")
            or "unknown"
        )
        posts.append(
            {
                "platform": "x",
                "external_id": tweet_id,
                "title": text[:120],
                "content": text,
                "url": tweet.get("url") or f"https://x.com/{author}/status/{tweet_id}",
                "author": author,
                "created_at": tweet.get("createdAt"),
                "likes": likes,
                "comments": replies,
            }
        )

    return posts[: payload["max_results"]]


def fetch_x(payload: dict) -> list[dict]:
    if not is_x_configured():
        raise RuntimeError("X is not configured (set X_BEARER_TOKEN or APIFY_API_TOKEN)")
    if X_BEARER_TOKEN:
        return fetch_x_official(payload)
    return fetch_x_apify(payload)


FETCHERS = {
    "reddit": fetch_reddit,
    "hacker_news": fetch_hackernews,
    "x": fetch_x,
}


def ai_providers() -> list[dict]:
    providers = []
    if os.getenv("OLLAMA_API_KEY"):
        providers.append(
            {
                "id": "ollama",
                "base_url": OLLAMA_BASE_URL,
                "api_key": os.getenv("OLLAMA_API_KEY"),
                "model": OLLAMA_MODEL,
            }
        )
    if os.getenv("GROQ_API_KEY"):
        providers.append(
            {
                "id": "groq",
                "base_url": GROQ_BASE_URL,
                "api_key": os.getenv("GROQ_API_KEY"),
                "model": GROQ_MODEL,
            }
        )
    return providers


def ai_chat(messages: list[dict], temperature: float = 0.2, max_tokens: int = 700) -> Optional[str]:
    for provider in ai_providers():
        try:
            response = requests.post(
                f"{provider['base_url']}/chat/completions",
                json={
                    "model": provider["model"],
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                headers={
                    "Authorization": f"Bearer {provider['api_key']}",
                    "Content-Type": "application/json",
                },
                timeout=60,
            )
            if not response.ok:
                print(f"[AI] {provider['id']} error {response.status_code}")
                continue
            content = (
                response.json().get("choices", [{}])[0].get("message", {}).get("content") or ""
            ).strip()
            if content:
                return content
        except Exception as exc:
            print(f"[AI] {provider['id']} failed: {exc}")
    return None


def score_heuristic(post: dict, ctx: dict) -> tuple[int, str]:
    text = f"{post['title']} {post.get('content') or ''}".lower()
    title = post["title"].lower()
    for kw in ctx["pos_keywords"]:
        kw_lower = kw.strip().lower()
        if not kw_lower:
            continue
        if kw_lower in title:
            return 78, "Keyword match in post title (AI scoring unavailable)."
        if kw_lower in text:
            return 72, "Keyword match in post content (AI scoring unavailable)."
    return 55, "No direct keyword match (AI scoring unavailable)."


def score_post(post: dict, ctx: dict) -> tuple[int, str]:
    if not ai_providers():
        return score_heuristic(post, ctx)

    prompt = "\n".join(
        [
            f"Campaign: {ctx['name']}",
            f"What we sell: {ctx.get('description') or '(no description)'}",
            f"Ideal customer: {ctx.get('target_description') or '(anyone with the problem we solve)'}",
            f"Exclude leads that are: {ctx.get('exclude_description') or '(nothing specific)'}",
            f"Positive keywords: {', '.join(ctx['pos_keywords']) or '(none)'}",
            f"Negative keywords: {', '.join(ctx['neg_keywords']) or '(none)'}",
            "",
            f"Lead post from {PLATFORM_LABELS.get(post['platform'], post['platform'])}:",
            f"Author: {post['author']}",
            f"Title: {post['title']}",
            f"Content: {(post.get('content') or '')[:1200] or '(no content)'}",
            "",
            "Score how strongly this person is a potential buyer for what the campaign sells.",
            'Return JSON: {"score": <integer 0-100>, "reason": "<one short sentence>"}',
            "score >= 70 only if the author shows real buying intent or describes the exact problem we solve.",
            "score <= 30 if the post is about a competitor, a job offer, hiring, or generic discussion without intent.",
        ]
    )

    raw = ai_chat(
        [
            {
                "role": "system",
                "content": (
                    "You are a B2B lead-scoring engine. You judge social posts for buying "
                    "intent with high precision. You always respond with valid JSON only."
                ),
            },
            {"role": "user", "content": prompt},
        ]
    )
    if not raw:
        return score_heuristic(post, ctx)

    cleaned = raw.replace("```json", "").replace("```", "").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        cleaned = cleaned[start : end + 1]
    try:
        parsed = json.loads(cleaned)
        score = max(0, min(100, int(round(float(parsed.get("score", 0))))))
        return score, str(parsed.get("reason", ""))[:300]
    except Exception:
        print(f"[AI] Could not parse scoring JSON: {cleaned[:160]}")
        return score_heuristic(post, ctx)


def matches_any(text: str, phrases: list[str]) -> bool:
    lower = text.lower()
    return any(phrase.lower() in lower for phrase in phrases if phrase.strip())


def insert_leads(
    cur: Any,
    campaign_id: str,
    posts: list[dict],
    has_platform: bool,
    has_reason: bool,
    limit: int,
) -> int:
    inserted = 0
    for post in posts:
        if inserted >= limit:
            break

        if has_platform:
            columns = [
                "campaign_id",
                "platform",
                "external_id",
                "title",
                "content",
                "url",
                "author",
                "ai_relevance_score",
            ]
            values = [
                campaign_id,
                post["platform"],
                post["external_id"],
                post["title"],
                post.get("content"),
                post["url"],
                post["author"],
                post["score"],
            ]
            if has_reason:
                columns.append("ai_score_reason")
                values.append(post.get("reason"))
            columns.append("status")
            values.append("new")

            placeholders = ", ".join(["%s"] * len(values))
            cur.execute(
                f"""
                INSERT INTO leads ({", ".join(columns)})
                VALUES ({placeholders})
                ON CONFLICT (campaign_id, platform, external_id) DO NOTHING
                RETURNING id
                """,
                values,
            )
        else:
            cur.execute(
                """
                INSERT INTO leads (campaign_id, reddit_post_id, title, content, url, author, ai_relevance_score, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (reddit_post_id) DO NOTHING
                RETURNING id
                """,
                (
                    campaign_id,
                    post["external_id"],
                    post["title"],
                    post.get("content"),
                    post["url"],
                    post["author"],
                    post["score"],
                    "new",
                ),
            )

        if cur.fetchone() is not None:
            inserted += 1
    return inserted


def scan_campaign(cur: Any, campaign: dict, has_platform: bool, has_reason: bool) -> dict:
    campaign_id = campaign["id"]
    plan = get_user_plan(cur, campaign["user_id"])

    cur.execute(
        "SELECT phrase, is_negative FROM keywords WHERE campaign_id = %s",
        (campaign_id,),
    )
    keyword_rows = cur.fetchall()
    pos_keywords = [row[0] for row in keyword_rows if not row[1]]
    neg_keywords = [row[0] for row in keyword_rows if row[1]]

    if not pos_keywords:
        mark_campaign_run(cur, campaign_id, "failed", "Campaign has no positive keywords.")
        return {"campaign_id": str(campaign_id), "status": "failed", "error": "No positive keywords"}

    requested = campaign.get("platforms") or ["reddit"]
    allowed = [p for p in requested if p in PLAN_LIMITS[plan]["platforms"]]
    if not allowed:
        message = f"Your {plan} plan does not include the selected platforms. Upgrade to unlock more."
        mark_campaign_run(cur, campaign_id, "failed", message)
        return {"campaign_id": str(campaign_id), "status": "failed", "error": message}

    remaining = get_remaining_lead_quota(cur, campaign["user_id"], plan)
    if remaining <= 0:
        message = (
            f"Monthly lead quota reached ({PLAN_LIMITS[plan]['leads_per_month']} leads on {plan}). "
            "Upgrade to continue scanning."
        )
        mark_campaign_run(cur, campaign_id, "failed", message)
        return {"campaign_id": str(campaign_id), "status": "failed", "error": message}

    fetch_payload = {
        "pos_keywords": pos_keywords,
        "neg_keywords": neg_keywords,
        "fallback_terms": derived_terms(
            campaign["name"],
            campaign.get("description"),
            campaign.get("target_description"),
            campaign.get("lead_type"),
        ),
        "time_filter_days": campaign.get("time_filter_days") or 7,
        "min_likes": campaign.get("min_likes") or 0,
        "min_comments": campaign.get("min_comments") or 0,
        "max_results": MAX_RESULTS_PER_PLATFORM,
    }

    collected: list[dict] = []
    platform_errors: list[str] = []
    skipped: list[str] = []

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {platform: pool.submit(FETCHERS[platform], fetch_payload) for platform in allowed}
        for platform, future in futures.items():
            try:
                collected.extend(future.result())
            except Exception as exc:
                label = PLATFORM_LABELS.get(platform, platform)
                message = str(exc)
                if "not configured" in message.lower():
                    skipped.append(f"{label}: {message}")
                else:
                    platform_errors.append(f"{label}: {message}")

    seen: set[str] = set()
    candidates = []
    for post in collected:
        key = f"{post['platform']}:{post['external_id']}"
        if key in seen:
            continue
        seen.add(key)
        haystack = f"{post['title']} {post.get('content') or ''}"
        if matches_any(haystack, neg_keywords):
            continue
        if not matches_any(haystack, pos_keywords):
            continue
        candidates.append(post)

    candidates = candidates[:MAX_CANDIDATES]

    ctx = {
        "name": campaign["name"],
        "description": campaign.get("description"),
        "target_description": campaign.get("target_description"),
        "exclude_description": campaign.get("exclude_description"),
        "pos_keywords": pos_keywords,
        "neg_keywords": neg_keywords,
    }

    scored = []
    if candidates:
        with ThreadPoolExecutor(max_workers=AI_WORKERS) as pool:
            results = list(pool.map(lambda post: score_post(post, ctx), candidates))
        for post, (score, reason) in zip(candidates, results):
            post["score"] = score
            post["reason"] = reason
            scored.append(post)

    qualified = [post for post in scored if post["score"] >= SCORE_THRESHOLD]
    qualified.sort(key=lambda post: post["score"], reverse=True)
    qualified = qualified[:remaining]

    inserted = insert_leads(cur, campaign_id, qualified, has_platform, has_reason, remaining)

    notes = []
    if skipped:
        notes.append("Skipped — " + "; ".join(skipped))
    if platform_errors:
        notes.append("Platform errors — " + "; ".join(platform_errors))
    if candidates and len(qualified) >= remaining and inserted >= remaining:
        notes.append("Lead quota reached — upgrade to capture more.")

    print(
        f"[Scan] Campaign '{campaign['name']}': "
        f"{len(candidates)} candidates, {len(qualified)} qualified, {inserted} saved"
    )

    if not collected and platform_errors:
        mark_campaign_run(cur, campaign_id, "failed", platform_errors[0])
        return {"campaign_id": str(campaign_id), "status": "failed", "error": platform_errors[0]}

    mark_campaign_run(cur, campaign_id, "success", " | ".join(notes) or None)
    return {
        "campaign_id": str(campaign_id),
        "status": "success",
        "inserted": inserted,
        "candidates": len(candidates),
        "qualified": len(qualified),
    }


@app.get("/")
def root() -> dict:
    return {"status": "ok", "version": app.version}


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": app.version,
        "ai_providers": [p["id"] for p in ai_providers()],
        "x_configured": is_x_configured(),
    }


@app.post("/run")
def run_scan(payload: RunRequest, x_api_key: Optional[str] = Header(default=None, alias="x-api-key")) -> dict:
    if SCANNER_API_KEY and x_api_key != SCANNER_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="DATABASE_URL is not configured")

    conn = None
    cur = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        has_platform = table_has_column(cur, "leads", "platform")
        has_reason = table_has_column(cur, "leads", "ai_score_reason")
        has_campaign_platforms = table_has_column(cur, "campaigns", "platforms")

        if payload.campaign_id:
            mark_campaign_run(cur, payload.campaign_id, "running")
            conn.commit()

        select_columns = [
            "id",
            "user_id",
            "name",
            "description",
            "target_description",
            "exclude_description",
            "lead_type",
            "time_filter_days",
            "min_likes",
            "min_comments",
        ]
        if has_campaign_platforms:
            select_columns.append("platforms")

        if payload.campaign_id:
            cur.execute(
                f"SELECT {', '.join(select_columns)} FROM campaigns WHERE id = %s",
                (payload.campaign_id,),
            )
        else:
            cur.execute(
                f"SELECT {', '.join(select_columns)} FROM campaigns ORDER BY created_at DESC LIMIT 5"
            )

        rows = cur.fetchall()
        if not rows:
            return {"status": "ok", "message": "No campaigns found", "processed": 0, "results": []}

        keys = select_columns
        campaigns = [dict(zip(keys, row)) for row in rows]

        results = []
        for campaign in campaigns:
            try:
                results.append(scan_campaign(cur, campaign, has_platform, has_reason))
            except Exception as exc:
                print(f"[Scan] Campaign {campaign['id']} failed: {exc}")
                mark_campaign_run(cur, campaign["id"], "failed", str(exc))
                results.append({"campaign_id": str(campaign["id"]), "status": "failed", "error": str(exc)})
            conn.commit()

            if len(campaigns) > 1:
                time.sleep(5)

        return {"status": "ok", "processed": len(campaigns), "results": results}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()