#!/usr/bin/env python3
"""
Job Hunter — fetches new-grad/0-exp SDE roles from multiple sources,
scores against profile.yaml, pushes to Notion Verified Job Tracker.
Runs as a cron script (no_agent=True) — zero LLM tokens.
"""

import os
import json
import sys
import re
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import yaml
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─── Config ───
PROFILE_PATH = Path(os.environ.get("HERMES_HOME", Path.home() / "AppData/Local/hermes")) / "profiles" / "profile.yaml"
NOTION_DB_ID = os.environ.get("NOTION_DB_ID", "").strip()
NOTION_TOKEN = os.environ.get("NOTION_API_KEY")
OUTPUT_DIR = Path(os.environ.get("HERMES_HOME", Path.home() / "AppData/Local/hermes")) / "cron" / "output"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 JobHunter/1.0",
    "Accept": "application/json",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
SESSION.mount("https://", HTTPAdapter(max_retries=retries))

# ─── Load Profile ───
with open(PROFILE_PATH) as f:
    PROFILE = yaml.safe_load(f)

TARGET_ROLES = [r.lower() for r in PROFILE.get("professional", {}).get("target_roles", [])]
CORE_STACK = [s.lower() for s in PROFILE.get("professional", {}).get("core_stack", [])]
GRAD_YEAR = PROFILE.get("education", {}).get("graduation_year", 2026)
LOCATION = PROFILE.get("personal", {}).get("location", "India")

# ─── Exp filter: reject 7+, 5+, 3+ years ───
EXP_RE = re.compile(r"(\d+)\+?\s*(?:years?|yrs?)\s*(?:of\s+)?(?:experience|exp)", re.I)
NEW_GRAD_KEYWORDS = [
    "new grad", "new-grad", "new graduate", "entry level", "entry-level",
    "fresher", "freshers", "0 year", "0 years", "0-1 year", "0-1 years",
    "0-2 year", "0-2 years", "2026 batch", "2026 graduate", "2026 grad",
    "campus", "intern", "internship", "trainee", "junior", "associate",
    "recent graduate", "recent grad", "grad program", "graduate program",
]

def is_eligible(text: str) -> bool:
    """Return True if role is eligible for new-grad/fresher path."""
    t = text.lower()
    # Hard block: explicit multi-year requirements
    for m in EXP_RE.finditer(t):
        years = int(m.group(1))
        if years >= 3:
            return False
    # Soft pass: new-grad keywords
    if any(kw in t for kw in NEW_GRAD_KEYWORDS):
        return True
    # If no exp mentioned, assume eligible (conservative)
    return True

def score_job(title: str, desc: str, company: str, location: str, source: str) -> tuple[int, str]:
    """Score 0-100, return (score, priority)."""
    text = f"{title} {desc} {company}".lower()
    score = 50  # base

    # Role match
    for role in TARGET_ROLES:
        if role in text:
            score += 15
            break

    # Stack match
    for tech in CORE_STACK:
        if tech.lower() in text:
            score += 5

    # CP achievements boost
    if any(kw in text for kw in ["competitive", "codeforces", "codechef", "icpc", "iicpc", "algorithmic", "data structures"]):
        score += 10

    # India / Remote preference
    loc = location.lower()
    if "india" in loc or "remote" in loc or "anywhere" in loc:
        score += 5

    # Internship / new-grad explicit
    if any(kw in text for kw in ["intern", "new grad", "fresher", "entry level", "2026"]):
        score += 10

    # Company tier boost (rough heuristic)
    tier1 = ["stripe", "uber", "airbnb", "databricks", "snowflake", "anthropic", "openai", "google", "meta", "amazon", "microsoft", "netflix", "figma", "notion", "linear", "vercel", "retool", "n8n", "elevenlabs"]
    if any(c in company.lower() for c in tier1):
        score += 8

    # Cap
    score = min(100, max(0, score))

    if score >= 80:
        priority = "P1 - Critical"
    elif score >= 70:
        priority = "P2 - High"
    elif score >= 55:
        priority = "P3 - Medium"
    else:
        priority = "P4 - Watchlist"
    return score, priority

def make_uid(source: str, title: str, company: str, url: str) -> str:
    raw = f"{source}|{title}|{company}|{url}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

# ─── Source Fetchers ───
def fetch_simplify_jobs():
    """SimplifyJobs New Grad 2026 GitHub repo (JSON)."""
    url = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"
    try:
        r = SESSION.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        out = []
        for job in data:
            title = job.get("title", "")
            company = job.get("company_name", "")
            locations = job.get("locations", [])
            url = job.get("url", "")
            desc = job.get("description", "")
            loc_str = ", ".join(locations) if isinstance(locations, list) else str(locations)
            if is_eligible(f"{title} {desc}"):
                out.append({"title": title, "company": company, "location": loc_str, "url": url, "description": desc, "source": "SimplifyJobs"})
        return out
    except Exception as e:
        return [{"error": f"SimplifyJobs: {e}"}]

def fetch_jobright():
    """Jobright.ai API (public search)."""
    # Jobright has a public search endpoint; fallback to scraping if needed
    url = "https://api.jobright.ai/v1/jobs/search"
    params = {"q": "new grad software engineer", "location": "India", "page": 1, "size": 20}
    try:
        r = SESSION.get(url, params=params, timeout=20)
        if r.status_code == 200:
            data = r.json()
            out = []
            for job in data.get("jobs", []):
                title = job.get("title", "")
                company = job.get("company", {}).get("name", "")
                location = job.get("location", "")
                url = job.get("apply_url", "") or job.get("url", "")
                desc = job.get("description", "")
                if is_eligible(f"{title} {desc}"):
                    out.append({"title": title, "company": company, "location": location, "url": url, "description": desc, "source": "Jobright.ai"})
            return out
    except Exception as e:
        pass
    return [{"error": f"Jobright.ai: fetch failed"}]

def fetch_vanshb03():
    """vanshb03 GitHub repo (India new grad)."""
    url = "https://raw.githubusercontent.com/vanshb03/New-Grad-Jobs-India/main/jobs.json"
    try:
        r = SESSION.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        out = []
        for job in data:
            title = job.get("title", "")
            company = job.get("company", "")
            location = job.get("location", "")
            url = job.get("apply_link", "") or job.get("link", "")
            desc = job.get("description", "")
            if is_eligible(f"{title} {desc}"):
                out.append({"title": title, "company": company, "location": location, "url": url, "description": desc, "source": "vanshb03 GitHub"})
        return out
    except Exception as e:
        return [{"error": f"vanshb03 GitHub: {e}"}]

def fetch_remoteok():
    """RemoteOK API (has tags for new-grad, internship)."""
    url = "https://remoteok.io/api"
    try:
        r = SESSION.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()
        out = []
        # First element is metadata
        for job in data[1:]:
            title = job.get("position", "")
            company = job.get("company", "")
            location = job.get("location", "")
            url = job.get("url", "")
            desc = job.get("description", "")
            tags = [t.lower() for t in job.get("tags", [])]
            # Filter by tags
            if any(t in tags for t in ["new-grad", "entry-level", "internship", "junior", "fresher"]):
                if is_eligible(f"{title} {desc}"):
                    out.append({"title": title, "company": company, "location": location, "url": url, "description": desc, "source": "RemoteOK"})
        return out
    except Exception as e:
        return [{"error": f"RemoteOK: {e}"}]

def fetch_portals():
    """Greenhouse/Lever/Ashby aggregator endpoints (public)."""
    # Wellknown aggregator endpoints for new-grad
    urls = [
        "https://boards-api.greenhouse.io/v1/boards?content=true",
        "https://api.lever.co/v0/postings/?mode=json",
    ]
    out = []
    for url in urls:
        try:
            r = SESSION.get(url, timeout=20)
            if r.status_code == 200:
                data = r.json()
                if "boards" in data:
                    for board in data["boards"]:
                        for job in board.get("jobs", []):
                            title = job.get("title", "")
                            desc = job.get("content", "") or job.get("description", "")
                            if is_eligible(f"{title} {desc}"):
                                out.append({
                                    "title": title,
                                    "company": board.get("name", ""),
                                    "location": ", ".join(job.get("location", {}).get("name", "") for _ in [1]) if job.get("location") else "",
                                    "url": job.get("absolute_url", ""),
                                    "description": desc,
                                    "source": "Greenhouse"
                                })
                elif isinstance(data, list):
                    for job in data:
                        title = job.get("text", "") or job.get("title", "")
                        desc = job.get("description", "")
                        if is_eligible(f"{title} {desc}"):
                            out.append({
                                "title": title,
                                "company": job.get("categories", {}).get("company", "") if job.get("categories") else "",
                                "location": job.get("categories", {}).get("location", "") if job.get("categories") else "",
                                "url": job.get("applyUrl", "") or job.get("hostedUrl", ""),
                                "description": desc,
                                "source": "Lever"
                            })
        except Exception:
            pass
    return out if out else [{"error": "Portals/Aggregators: no data"}]

def fetch_linkedin_camofox():
    """Use Camofox browser to scrape LinkedIn searches (requires running Camofox)."""
    # This is a placeholder — real impl would use browser automation.
    # For now, return known working search URLs as "jobs" so user can click.
    searches = [
        ("SDE Intern India 2026", "https://www.linkedin.com/jobs/search/?keywords=SDE%20Intern%20India%202026&f_E=1&f_TPR=r604800"),
        ("Backend Intern India 2026", "https://www.linkedin.com/jobs/search/?keywords=Backend%20Intern%20India%202026&f_E=1&f_TPR=r604800"),
        ("New Grad Software Engineer India", "https://www.linkedin.com/jobs/search/?keywords=New%20Grad%20Software%20Engineer%20India&f_E=1&f_TPR=r604800"),
    ]
    out = []
    for name, url in searches:
        out.append({
            "title": name,
            "company": "LinkedIn Search",
            "location": "India",
            "url": url,
            "description": f"LinkedIn search for {name}",
            "source": "LinkedIn (Camofox)"
        })
    return out

# ─── Notion Push ───
def push_to_notion(jobs: list[dict]) -> tuple[int, int]:
    """Push jobs to Notion database. Returns (pushed, errors)."""
    if not NOTION_TOKEN:
        print("ERROR: NOTION_API_KEY not set", file=sys.stderr)
        return 0, len(jobs)

    notion_headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    # Query existing titles+urls to dedupe
    existing = set()
    has_more = True
    next_cursor = None
    while has_more:
        payload = {"page_size": 100}
        if next_cursor:
            payload["start_cursor"] = next_cursor
        try:
            r = SESSION.post(
                f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
                headers=notion_headers,
                json=payload,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            for page in data.get("results", []):
                props = page.get("properties", {})
                title_prop = props.get("Company & Role", {}).get("title", [])
                url_prop = props.get("Direct Link", {}).get("url", "")
                if title_prop:
                    title = title_prop[0]["text"]["content"]
                    existing.add(f"{title}|{url_prop}")
            has_more = data.get("has_more", False)
            next_cursor = data.get("next_cursor")
        except Exception as e:
            print(f"Notion query error: {e}", file=sys.stderr)
            break

    pushed = 0
    errors = 0
    for job in jobs:
        if "error" in job:
            continue
        title_key = f"{job['company']} — {job['title']}"
        url = job.get("url", "")
        dedupe_key = f"{title_key}|{url}"
        if dedupe_key in existing:
            continue

        score, priority = score_job(
            job["title"], job.get("description", ""), job["company"],
            job.get("location", ""), job["source"]
        )

        props = {
            "Company & Role": {"title": [{"text": {"content": title_key}}]},
            "Direct Link": {"url": url},
            "Score": {"number": score},
            "Priority": {"select": {"name": priority}},
        }

        try:
            r = SESSION.post(
                "https://api.notion.com/v1/pages",
                headers=notion_headers,
                json={"parent": {"database_id": NOTION_DB_ID}, "properties": props},
                timeout=30,
            )
            if r.status_code in (200, 201):
                pushed += 1
                existing.add(dedupe_key)
                time.sleep(2.5)  # Rate limit mitigation - Notion allows ~3 req/sec
            elif r.status_code == 429:
                errors += 1
                retry_after = int(r.json().get("additional_data", {}).get("retry_after", "5"))
                print(f"Notion rate limited, waiting {retry_after}s...", file=sys.stderr)
                time.sleep(min(retry_after + 1, 30))
            else:
                errors += 1
                print(f"Notion push failed: {r.status_code} {r.text}", file=sys.stderr)
        except Exception as e:
            errors += 1
            print(f"Notion push exception: {e}", file=sys.stderr)

    return pushed, errors

# ─── Main ───
def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"JOB HUNT CYCLE — {datetime.now().strftime('%d %b %Y %H:%M')}")
    all_jobs = []
    source_errors = []
    fetch_counts = {}

    fetchers = [
        ("SimplifyJobs", fetch_simplify_jobs),
        ("Jobright.ai", fetch_jobright),
        ("vanshb03 GitHub", fetch_vanshb03),
        ("RemoteOK", fetch_remoteok),
        ("Portals/Aggregators", fetch_portals),
        ("LinkedIn (Camofox)", fetch_linkedin_camofox),
    ]

    for name, fn in fetchers:
        try:
            jobs = fn()
            valid = [j for j in jobs if "error" not in j]
            fetch_counts[name] = len(valid)
            all_jobs.extend(valid)
            for j in jobs:
                if "error" in j:
                    source_errors.append(j["error"])
        except Exception as e:
            source_errors.append(f"{name}: {e}")
            fetch_counts[name] = 0

    # Deduplicate by UID
    seen = set()
    unique_jobs = []
    for job in all_jobs:
        uid = make_uid(job["source"], job["title"], job["company"], job.get("url", ""))
        if uid not in seen:
            seen.add(uid)
            unique_jobs.append(job)

    # Push to Notion
    pushed, push_errors = push_to_notion(unique_jobs)
    skipped = len(unique_jobs) - pushed - push_errors

    # Summary
    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "fetched": fetch_counts,
        "fetched_total": sum(fetch_counts.values()),
        "unique": len(unique_jobs),
        "pushed": pushed,
        "skipped": skipped,
        "push_errors": push_errors,
        "source_errors": source_errors,
        "pushed_jobs": [],
    }

    # Add top pushed jobs to summary
    for job in unique_jobs[:50]:
        uid = make_uid(job["source"], job["title"], job["company"], job.get("url", ""))
        score, priority = score_job(
            job["title"], job.get("description", ""), job["company"],
            job.get("location", ""), job["source"]
        )
        summary["pushed_jobs"].append({
            "company_role": f"{job['company']} — {job['title']}",
            "score": score,
            "priority": priority,
            "source": job["source"],
            "location": job.get("location", ""),
        })

    # Write outputs
    json_path = OUTPUT_DIR / f"job_hunt_run_{run_id}.json"
    txt_path = OUTPUT_DIR / f"job_hunt_run_{run_id}.txt"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # Human-readable text
    lines = [
        f"JOB HUNT CYCLE — {datetime.now().strftime('%d %b %Y %H:%M')}",
        f"Fetched: {summary['fetched_total']} qualifying across {len(fetch_counts)} sources",
        f"Unique: {summary['unique']} | Pushed: {pushed} | Skipped (dup): {skipped} | Errors: {push_errors}",
        "",
    ]
    for j in summary["pushed_jobs"]:
        lines.append(f"[{j['priority']}] {j['company_role']} ({j['source']}) — score {j['score']}")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # Print to stdout (captured by cron)
    print("\n".join(lines))

    # Exit code 0 always — cron records state in executions.db
    return 0

if __name__ == "__main__":
    sys.exit(main())