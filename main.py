import os
import re
import sys
import time
from pathlib import Path
from typing import Optional
import requests
import psycopg2
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv(Path(__file__).resolve().parent / ".env")

app = FastAPI(title="EchoLeads Python API", version="1.0.0")

DATABASE_URL = os.getenv("DATABASE_URL")


class RunRequest(BaseModel):
    campaign_id: Optional[str] = None


class LeadPayload(BaseModel):
    campaign_id: str
    reddit_post_id: str
    title: str
    content: str
    url: str
    author: str
    ai_relevance_score: int
    status: str = "new"


@app.get("/")
def root() -> dict:
    return {"status": "ok"}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def mark_campaign_run(cur, campaign_id: Optional[str], status: str, error: Optional[str] = None) -> None:
    """Update a campaign's last run status in the database."""
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


@app.post("/run")
def run_scan(payload: RunRequest) -> dict:
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="DATABASE_URL is not configured")

    conn = None
    cur = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        if payload.campaign_id:
            mark_campaign_run(cur, payload.campaign_id, "running")
            conn.commit()

        cur.execute(
            """
            SELECT id, name, description, target_description, exclude_description, lead_type, time_filter_days, min_likes, min_comments
            FROM campaigns
            WHERE (%s::uuid IS NULL OR id = %s)
            ORDER BY created_at DESC
            LIMIT 5
            """,
            (payload.campaign_id, payload.campaign_id),
        )
        campaigns = cur.fetchall()

        if not campaigns:
            return {"status": "ok", "message": "No campaigns found"}

        results = []
        for campaign in campaigns:
            campaign_id, name, description, target_description, exclude_description, lead_type, time_filter_days, min_likes, min_comments = campaign
            campaign_error = None

            try:
                # Fetch keywords for this campaign
                cur.execute("SELECT phrase, is_negative FROM keywords WHERE campaign_id = %s", (campaign_id,))
                keywords_rows = cur.fetchall()
                pos_keywords = [row[0].lower() for row in keywords_rows if not row[1]]
                neg_keywords = [row[0].lower() for row in keywords_rows if row[1]]

                search_query = build_search_query(name, description, target_description, lead_type, pos_keywords, neg_keywords)

                t_param = "all"
                if time_filter_days:
                    if time_filter_days <= 1:
                        t_param = "day"
                    elif time_filter_days <= 7:
                        t_param = "week"
                    elif time_filter_days <= 31:
                        t_param = "month"
                    elif time_filter_days <= 365:
                        t_param = "year"

                json_url = f"https://www.reddit.com/search.rss?q={requests.utils.quote(search_query)}&sort=new&t={t_param}&limit=100"

                response = None
                for attempt in range(1, 4):
                    try:
                        response = requests.get(json_url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36 EchoLeads/1.0"})
                        response.raise_for_status()
                        break
                    except requests.exceptions.HTTPError as exc:
                        if response is not None and response.status_code == 429 and attempt < 3:
                            wait_secs = 10 * attempt
                            print(f"[Scan] Reddit 429 for '{name}', retry {attempt}/3 in {wait_secs}s")
                            time.sleep(wait_secs)
                            continue
                        campaign_error = f"Reddit fetch failed: {exc}"
                        print(f"Reddit fetch failed for {name}: {exc}")
                        mark_campaign_run(cur, campaign_id, "failed", campaign_error)
                        conn.commit()
                        results.append({"campaign_id": str(campaign_id), "status": "failed", "error": campaign_error})
                        response = None
                        break
                    except Exception as exc:
                        campaign_error = f"Reddit fetch failed: {exc}"
                        print(f"Reddit fetch failed for {name}: {exc}")
                        mark_campaign_run(cur, campaign_id, "failed", campaign_error)
                        conn.commit()
                        results.append({"campaign_id": str(campaign_id), "status": "failed", "error": campaign_error})
                        response = None
                        break

                if response is None:
                    continue

                from bs4 import BeautifulSoup
                soup = BeautifulSoup(response.text, "xml")
                entries = soup.find_all("entry")
                print(f"[Scan] Campaign '{name}': found {len(entries)} Reddit posts")

                cutoff_time = 0
                if time_filter_days:
                    cutoff_time = time.time() - (time_filter_days * 86400)

                scored_count = 0
                saved_count = 0
                for entry in entries:
                    from datetime import datetime
                    published = entry.published.get_text() if entry.published else ""
                    created_utc = 0
                    if published:
                        try:
                            # Replace Z with +00:00 for python 3.7+ compatibility
                            pub = published.replace("Z", "+00:00")
                            created_utc = datetime.fromisoformat(pub).timestamp()
                        except Exception as e:
                            pass

                    if created_utc and created_utc < cutoff_time:
                        continue

                    title = clean_text(entry.title.get_text() if entry.title else "")
                    content = clean_text(entry.content.get_text() if entry.content else "")
                    author = clean_text(entry.author.find("name").get_text() if entry.author and entry.author.find("name") else "")
                    url = entry.link.get("href", "") if entry.link else ""
                    post_id = entry.id.get_text() if entry.id else ""

                    if not title and not content:
                        continue

                    # Local negative keyword filtering
                    text_lower = f"{title} {content}".lower()
                    if any(neg in text_lower for neg in neg_keywords):
                        continue

                    relevance = score_relevance(title, content, description, target_description, pos_keywords, exclude_description)
                    scored_count += 1

                    if relevance < 70:
                        continue

                    saved_count += 1
                    print(f"[Scan] Lead saved (score {relevance}): {title[:80]}")
                    cur.execute(
                        """
                        INSERT INTO leads (campaign_id, reddit_post_id, title, content, url, author, ai_relevance_score, status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (reddit_post_id) DO NOTHING
                        """,
                        (campaign_id, post_id, title, content, url, author, relevance, "new"),
                    )

                print(f"[Scan] Campaign '{name}': scored {scored_count}, saved {saved_count} leads")
                mark_campaign_run(cur, campaign_id, "success")
                results.append({"campaign_id": str(campaign_id), "status": "success"})
            except Exception as exc:
                campaign_error = str(exc)
                mark_campaign_run(cur, campaign_id, "failed", campaign_error)
                results.append({"campaign_id": str(campaign_id), "status": "failed", "error": campaign_error})

            conn.commit()
            if len(campaigns) > 1:
                time.sleep(5) # delay between campaigns to avoid 429 Too Many Requests

        return {"status": "ok", "processed": len(campaigns), "results": results}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


def build_search_query(name: str, description: Optional[str], target_description: Optional[str], lead_type: Optional[str], pos_keywords: list[str], neg_keywords: list[str]) -> str:
    query_parts = []

    if pos_keywords:
        # Take up to 6 positive keywords for the query
        phrases = [f'"{kw}"' for kw in pos_keywords[:6]]
        query_parts.append("(" + " OR ".join(phrases) + ")")
    else:
        seed_terms = []
        for value in [name, description, target_description]:
            if value:
                seed_terms.extend(re.split(r"[^a-z0-9]+", value.lower()))

        extra_terms = ["freelancer", "developer", "hire", "saas", "startup"] if lead_type == "service" else ["saas", "software", "tool", "startup"]
        terms = [t for t in set(seed_terms + extra_terms) if len(t) > 2 and t not in {"the", "and", "for", "with", "that", "this", "your", "help", "looking"}]
        terms = terms[:6]
        if terms:
            query_parts.append("(" + " OR ".join(f'"{term}"' for term in terms) + ")")
        else:
            query_parts.append("saas")

    if neg_keywords:
        # Add up to 3 negative keywords to the query
        for neg in neg_keywords[:3]:
            query_parts.append(f'-"{neg}"')

    return " ".join(query_parts)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def score_relevance(title: str, content: str, description: Optional[str], target_description: Optional[str], pos_keywords: list[str], exclude_description: Optional[str]) -> int:
    text = f"{title} {content}".lower()

    matches = 0
    if pos_keywords:
        for kw in pos_keywords:
            kw_lower = kw.lower().strip()
            if not kw_lower:
                continue
            # Exact phrase match (highest score)
            if kw_lower in text:
                matches += 40
            else:
                # Partial match: all significant words in the keyword appear in text
                words = [w for w in re.split(r"[^a-z0-9]+", kw_lower) if len(w) > 2]
                if words and all(w in text for w in words):
                    matches += 25
    else:
        if "freelancer" in text:
            matches += 20
        if "developer" in text:
            matches += 20
        if "saas" in text:
            matches += 15
        if "hire" in text:
            matches += 10
        if "need" in text:
            matches += 10

    return min(100, matches + 40) if matches > 0 else 0
