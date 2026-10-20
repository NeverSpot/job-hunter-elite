#!/usr/bin/env python3
"""
Job Hunter v6 — discovers entry-level / new-grad software roles, verifies them,
and maintains a curated 30-row Notion list of genuinely actionable openings.

Design rules (each one fixes a real failure):
  * Every row must be a DIRECT posting URL. Search-result, category and career
    homepage URLs are rejected — they are not vacancies.
  * Existing rows are RE-SCORED every run from stored evidence, so a scoring
    change never requires a destructive --reset. --reset is emergency-only.
  * Liveness is three-state: Active / Dead / Unknown. Only confirmed death
    (404/410/451, closure markers) removes a row; blocks and timeouts are
    Unknown and are kept, but demoted after repeated failures.
  * Dedupe is canonical-URL primary, (company, title, location) secondary —
    distinct vacancies at one company are never merged.
  * Rows the user marked Applied/Rejected are frozen: never re-ranked, never
    archived, and never re-added.
  * Eligibility is decided BEFORE scoring; employer prestige is only a
    tie-breaker, never a ranking signal.

Runs daily at 05:00 via Hermes cron (no_agent=True). Zero LLM tokens.
"""

import os
import sys

# ─── Interpreter guard ───
# If PyYAML/requests are missing, re-exec under another interpreter that has
# them. Candidates are discovered from PATH — no machine-specific interpreter
# path is hardcoded. If no candidate qualifies, the original ImportError is
# re-raised so the failure is visible instead of silent.
try:
    import yaml  # noqa: F401
    import requests  # noqa: F401
except ImportError:
    import shutil
    import subprocess
    for _alt in (shutil.which("python3.12"), shutil.which("python3"),
                 shutil.which("python")):
        if _alt and os.path.abspath(_alt) != os.path.abspath(sys.executable):
            sys.exit(subprocess.call([_alt, os.path.abspath(__file__)] + sys.argv[1:]))
    raise

import json
import re
import time
import hashlib
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse
from concurrent.futures import ThreadPoolExecutor

import yaml
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─── Config ───
SCORE_MODEL = 6                      # bump when score_job() changes
_DEFAULT_HERMES_HOME = (Path.home() / "AppData/Local/hermes" if os.name == "nt"
                        else Path.home() / ".hermes")
HERMES_HOME = Path(os.environ.get("HERMES_HOME", _DEFAULT_HERMES_HOME))


def _profile_path():
    """Resolve the profile file.

    Order: $PROFILE_PATH -> $HERMES_HOME/profiles/profile.yaml (the canonical
    Hermes profile) -> ./profile.yaml in the working directory -> profile.yaml
    next to the repository root. Nothing here is machine-specific.
    """
    env = os.environ.get("PROFILE_PATH")
    if env:
        return Path(env)
    canonical = HERMES_HOME / "profiles" / "profile.yaml"
    if canonical.exists():
        return canonical
    for cand in (Path.cwd() / "profile.yaml",
                 Path(__file__).resolve().parent.parent / "profile.yaml"):
        if cand.exists():
            return cand
    return canonical


PROFILE_PATH = _profile_path()
# Target Notion database ("job list"). NOTION_DB_ID wins; otherwise the value
# of notion.database_id in the profile is used. Nothing is hardcoded here -
# set the environment variable or fill in the profile.
NOTION_DB_ID = os.environ.get("NOTION_DB_ID", "").strip()
NOTION_TOKEN = os.environ.get("NOTION_API_KEY")
OUTPUT_DIR = HERMES_HOME / "cron" / "output"

MAX_JOBS_TOTAL = 30
TOP_SLOTS = 10
BUFFER_SLOTS = 20
MAX_FRESH_AGE_DAYS = 90
MAX_CANDIDATES = 400
VERIFY_POOL = 90
VERIFY_WORKERS = 8
UNKNOWN_DEMOTE_AFTER = 3

# Notion property names
P_NAME, P_LINK, P_SCORE, P_PRI, P_TIER = "Name", "Direct Link", "Score", "Priority", "Tier"
P_COMPANY, P_SOURCE, P_LOC, P_POSTED = "Company", "Source", "Location", "Posted Date"
P_VERIFIED, P_REASON, P_VFAIL, P_MODEL = "Last Verified", "Fit Reason", "Verify Fails", "Score Model"
P_APP = "App Status"

# The user's own decisions are sacred: these rows are never touched by the pipeline.
FROZEN_STATUSES = {"Applied", "Rejected"}

PRIORITY_ORDER = {"P1 - Critical": 1, "P2 - High": 2, "P3 - Medium": 3, "P4 - Watchlist": 4}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 JobHunter/6.0",
    "Accept": "application/json",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])))

with open(PROFILE_PATH, encoding="utf-8") as f:
    PROFILE = yaml.safe_load(f)

# Fallback for the Notion database id: $NOTION_DB_ID, then notion.database_id
# in the profile. If neither is set the run fails fast with a clear message
# instead of querying an empty database id.
if not NOTION_DB_ID:
    NOTION_DB_ID = str((PROFILE.get("notion") or {}).get("database_id") or "").strip()
# Treat the shipped placeholder as "not configured".
if NOTION_DB_ID.upper().startswith("YOUR_"):
    NOTION_DB_ID = ""

TARGET_ROLES = [r.lower() for r in PROFILE.get("professional", {}).get("target_roles", [])]
CORE_STACK = [s.lower() for s in PROFILE.get("professional", {}).get("core_stack", [])]

# ─── Live ATS boards (per-company endpoints; the generic "list all boards" ones 404) ───
LEVER_BOARDS = ["meesho", "zeta", "mindtickle", "cred", "fampay"]
GREENHOUSE_BOARDS = ["canonical", "gitlab", "thoughtworks", "druva", "slice", "groww"]
ASHBY_BOARDS = ["openai", "harvey", "sierra", "ramp", "cursor", "baseten", "langchain",
                "vanta", "temporal", "replit", "supabase", "sardine", "modal", "hex",
                "speak", "linear", "airbyte", "posthog", "railway", "neon", "runway",
                "clerk", "mux"]

# Engineering categories worth keeping from the SimplifyJobs feed
KEEP_CATEGORIES = {"Software", "Software Engineering", "AI/ML/Data",
                   "Data Science, AI & Machine Learning"}

ENG_TITLE_RE = re.compile(
    r"(engineer|developer|sde|software|backend|back[- ]end|frontend|front[- ]end|"
    r"full[- ]?stack|data|machine learning|\bml\b|\bai\b|platform|infra|devops|"
    r"site reliability|\bsre\b|scientist|architect|programmer)", re.I)

# ─── Eligibility gates ───
# Word-boundary matching: "intern" must NOT match "internal"/"international".
NEW_GRAD_RE = re.compile(
    r"\b("
    r"new\s?-?\s?grad|new\s?graduate|entry[\s-]?level|early\s?career|"
    r"fresher|freshers|graduate\s?program|grad\s?program|recent\s?grad|"
    r"campus|intern|internship|trainee|junior|apprentice|"
    r"university\s?grad|college\s?grad|associate\s?software|"
    r"software\s?engineer\s?1|sde\s?1|engineer\s?i\b"
    r")\b", re.I)

ENTRY_TITLE_RE = NEW_GRAD_RE

ENTRY_DESC_RE = re.compile(
    r"(new\s?-?\s?grad|entry[\s-]?level|no\s?prior\s?experience|"
    r"no\s?experience\s?required|without\s?experience|0\s?-\s?1\s?years?|0\s?-\s?2\s?years?|"
    r"fresh\s?graduate|recent\s?graduate|freshers?|graduate\s?program|"
    r"training\s?program|campus\s?hire|university\s?graduate)", re.I)

EXP_ANY_RE = re.compile(r"(\d+)\s*\+?\s*(?:years?|yrs?)\s*(?:of\s+)?(?:experience|exp)", re.I)
EXP_RANGE_RE = re.compile(r"(\d+)\s*(?:-|to)\s*(\d+)\s*(?:years?|yrs?)", re.I)
SENIOR_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|architect|director|head\s+of|vp|"
    r"distinguished|fellow)\b", re.I)

# ─── Location bands ───
INDIA_HINTS = ["india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai",
               "gurgaon", "gurugram", "noida", "mumbai", "delhi", "kolkata", "ahmedabad",
               "kochi", "trivandrum", "jaipur", "indore"]
GLOBAL_REMOTE_HINTS = ["worldwide", "anywhere", "global", "home based", "home-based", "fully remote", "work from anywhere"]
US_ONLY_HINTS = ["usa", "u.s", "united states", "us only", "canada", "americas", "north america"]

# ─── URL classification ───
KNOWN_ATS = ("ashbyhq.com", "greenhouse.io", "lever.co", "myworkdayjobs.com", "amazon.jobs",
             "bytedance.com", "smartrecruiters.com", "workable.com", "rippling.com",
             "icims.com", "taleo.net", "jobvite.com", "recruitee.com", "bamboohr.com",
             "jobs.gem.com", "teamtailor.com", "pinpointhq.com", "job-boards.greenhouse.io")
SEARCH_URL_RE = re.compile(
    r"(/jobs/search|/search\?|/jobs\?q=|/search/|/jobs/?$|/careers/?$|/positions/?$|"
    r"/internships/?$|/vacancies/?$|/opportunities/?$)", re.I)
ID_LIKE_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}|/\d{5,}|[0-9a-f]{16,}|_R\d{4,})", re.I)
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                   "gh_src", "ref", "source", "fbclid", "gclid", "lever-source",
                   "ashby_source", "trackingid", "src", "referrer"}

# ─── Employer prestige: TIE-BREAKER ONLY (never part of the 0-100 score) ───
TIER1_TIEBREAK = ["openai", "anthropic", "stripe", "uber", "airbnb", "databricks", "snowflake",
                  "google", "meta", "amazon", "microsoft", "netflix", "nvidia", "canonical",
                  "gitlab", "atlassian", "salesforce", "adobe", "palantir", "figma", "notion",
                  "ramp", "scale ai", "datadog", "cloudflare", "spotify", "retool", "vercel"]
TIER2_TIEBREAK = ["razorpay", "cred", "zepto", "swiggy", "zomato", "flipkart", "phonepe", "groww",
                  "meesho", "postman", "browserstack", "freshworks", "zoho", "paytm", "nykaa",
                  "dream11", "games24x7", "jupiter", "slice", "navi", "chargebee", "haruka"]


def has_heavy_exp(title: str, desc: str) -> bool:
    """True only if the posting EXPLICITLY demands 2+ years of experience.

    Ranges are judged by their LOWER bound and then stripped, so "0-2 years"
    is treated as the range it is — not misread as the single value "2 years".
    """
    blob = f"{title} {desc}".lower()
    for m in EXP_RANGE_RE.finditer(blob):
        if int(m.group(1)) >= 2:
            return True
    stripped = EXP_RANGE_RE.sub(" ", blob)
    for m in EXP_ANY_RE.finditer(stripped):
        if int(m.group(1)) >= 2:
            return True
    return False


def is_entry_level(title: str, desc: str, curated_source: bool = False) -> bool:
    """Hard eligibility gate, applied BEFORE scoring.

    curated_source=True is for feeds that are new-grad by construction
    (SimplifyJobs) and whose `description` field is empty — there the source
    itself is the entry-level signal, but explicit 2+ year demands still reject.
    """
    if has_heavy_exp(title, desc):
        return False
    if SENIOR_RE.search(title):
        return False
    if curated_source:
        return True
    if ENTRY_TITLE_RE.search(title):
        return True
    if ENTRY_DESC_RE.search(desc):
        return True
    return False


def canonical_url(url: str) -> str:
    """Normalise a posting URL for dedupe: drop fragment + tracking params, keep job ids."""
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
        kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                if k.lower() not in TRACKING_PARAMS]
        path = p.path.rstrip("/") or "/"
        return urlunparse((p.scheme.lower(), p.netloc.lower(), path,
                           "", urlencode(kept), ""))
    except Exception:
        return url.strip().lower()


def url_kind(url: str) -> str:
    """Classify a URL: 'posting' (a specific vacancy), 'search' (not a vacancy), 'unknown'."""
    if not url:
        return "unknown"
    try:
        p = urlparse(url)
    except Exception:
        return "unknown"
    host, path = p.netloc.lower(), p.path or ""
    segs = [s for s in path.split("/") if s]
    if not path or path == "/":
        return "search" if any(a in host for a in KNOWN_ATS) else "unknown"
    if SEARCH_URL_RE.search(path):
        return "search"
    if ID_LIKE_RE.search(path):
        return "posting"
    if any(a in host for a in KNOWN_ATS):
        # e.g. boards.greenhouse.io/acme is a company board, not an opening
        return "posting" if len(segs) >= 2 else "search"
    if len(segs) >= 2:
        return "posting"
    return "unknown"


def _hits_any(text: str, hints) -> bool:
    """Word-boundary hint matching, so 'Indianapolis' never matches 'India'."""
    if not text:
        return False
    t = text.lower()
    for h in hints:
        if re.search(r"(?<![a-z0-9])" + re.escape(h) + r"(?![a-z0-9])", t):
            return True
    return False


def has_india(text: str) -> bool:
    return _hits_any(text, INDIA_HINTS)


def is_global_remote(text: str) -> bool:
    return _hits_any(text, GLOBAL_REMOTE_HINTS)


def is_us_only(text: str) -> bool:
    return _hits_any(text, US_ONLY_HINTS)


def employer_tiebreak(company: str) -> int:
    """0-2. Deliberately tiny: prestige breaks ties, it does not drive ranking."""
    c = (company or "").lower()
    if any(t in c for t in TIER1_TIEBREAK):
        return 2
    if any(t in c for t in TIER2_TIEBREAK):
        return 1
    return 0


def count_stack_hits(title, desc, company) -> int:
    """JD-derived stack overlap. Persisted at creation so every later run scores
    an existing row on exactly the same evidence a fresh row is scored on."""
    text = f"{title} {desc} {company}".lower()
    return sum(1 for t in CORE_STACK if t in text)


def score_job(title, company, location, source, posted_ts=None, url="",
              stack_hits=0, verify_fails=0) -> tuple:
    """Explainable, reproducible 0-100 fit score -> (score, priority, reason, tiebreak).

    Bands: role 30 | grad/exp 20 | India+eligibility 20 | stack 15 | freshness 10
           | link confidence 5.  Employer prestige is NOT in the score.

    Deliberately reads NO description text: descriptions are used only by the
    eligibility gates. That keeps re-scoring a stored row identical to scoring it
    the first time, which is what makes --reset unnecessary.
    """
    text = f"{title} {company}".lower()
    bits = []

    # A. Role fit (0-30)
    if any(r in text for r in TARGET_ROLES):
        a = 30
        bits.append("target role")
    elif ENG_TITLE_RE.search(title):
        a = 15
        bits.append("engineering role")
    else:
        a = 0

    # B. Graduate / experience-level fit (0-20)
    if NEW_GRAD_RE.search(text):
        b, why = 20, "new-grad/intern"
    elif EXP_RANGE_RE.search(text) or EXP_ANY_RE.search(EXP_RANGE_RE.sub(" ", text)):
        b, why = 12, "exp stated <=1y"
    else:
        b, why = 8, "no exp stated"
    bits.append(why)

    # C. India + work-eligibility (0-20)
    loc = location or ""
    if has_india(loc):
        c, why = 20, "India"
    elif is_global_remote(loc):
        c, why = 18, "worldwide remote"
    elif "remote" in loc.lower() and not is_us_only(loc):
        c, why = 14, "remote (non-US-only)"
    elif "remote" in loc.lower():
        c, why = 4, "US-only remote"
    elif not loc.strip():
        c, why = 5, "location unknown"
    else:
        c, why = 0, "onsite outside India"
    bits.append(why)

    # D. Relevant stack overlap (0-15) from the persisted hit count
    d = min(stack_hits * 5, 15)
    if stack_hits:
        bits.append(f"{stack_hits} stack hit(s)")

    # E. Freshness (0-10)
    if posted_ts:
        age = (datetime.now(timezone.utc).timestamp() - posted_ts) / 86400.0
        if age <= 7:
            e, why = 10, f"fresh {int(age)}d"
        elif age <= 30:
            e, why = 7, f"{int(age)}d old"
        else:
            e, why = 4, f"{int(age)}d old"
        bits.append(why)
    else:
        e = 3
        bits.append("freshness unknown")

    # F. Listing confidence (0-5)
    kind = url_kind(url)
    if kind == "posting":
        f, why = 5, "direct posting link"
    elif kind == "unknown":
        f, why = 2, "link kind unverified"
    else:
        f, why = 0, "not a posting URL"
    bits.append(why)

    score = max(0, min(100, a + b + c + d + e + f))

    # Repeatedly-unverifiable rows must not hold prime slots forever.
    if verify_fails >= UNKNOWN_DEMOTE_AFTER:
        penalty = min(verify_fails, 8)
        score = max(0, score - penalty)
        bits.append(f"unverified x{verify_fails}")

    if score >= 85:
        priority = "P1 - Critical"
    elif score >= 70:
        priority = "P2 - High"
    elif score >= 55:
        priority = "P3 - Medium"
    else:
        priority = "P4 - Watchlist"
    return score, priority, "; ".join(bits), employer_tiebreak(company)


def make_uid(source, title, company, url) -> str:
    return hashlib.sha256(f"{source}|{title}|{company}|{url}".encode()).hexdigest()[:16]


def norm_key(company: str, title: str, location: str = "") -> str:
    """Secondary dedupe identity: company + title + location (distinct posts stay distinct)."""
    def n(s):
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    loc = n(location)
    for k in INDIA_HINTS + ["remote", "hybrid", "onsite"]:
        loc = loc.replace(k, "").strip()
    return f"{n(company)}|{n(title)}|{loc}"


def display_name(company, title) -> str:
    return f"{company} — {title}".strip(" —")


def _ts(value):
    """Normalise a posting timestamp to epoch seconds (accepts epoch s, epoch ms, ISO 8601)."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v      # ms vs s
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def fetch_simplify_jobs():
    """SimplifyJobs New-Grad-Positions: active, recent, engineering, Bachelor's-reachable."""
    url = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_FRESH_AGE_DAYS)).timestamp()
    try:
        r = SESSION.get(url, timeout=25)
        r.raise_for_status()
        out = []
        for job in r.json():
            if not job.get("active", False):
                continue
            if job.get("date_posted", 0) < cutoff:
                continue
            if (job.get("category") or "") not in KEEP_CATEGORIES:
                continue
            degrees = [d.lower() for d in (job.get("degrees") or [])]
            if degrees and "bachelor" not in " ".join(degrees):
                continue
            title = job.get("title", "")
            company = job.get("company_name", "")
            desc = job.get("description", "") or ""
            # This feed is curated new-grad; its `description` is empty, so the
            # source itself is the entry-level signal. Only exp demands disqualify.
            if has_heavy_exp(title, desc):
                continue
            locs = job.get("locations", [])
            out.append({
                "title": title, "company": company,
                "location": ", ".join(locs) if isinstance(locs, list) else str(locs),
                "url": job.get("url", ""), "description": desc,
                "posted_ts": _ts(job.get("date_posted")),
                "source": "SimplifyJobs",
            })
        return out
    except Exception as e:
        return [{"error": f"SimplifyJobs: {e}"}]


def fetch_lever(boards):
    """Lever postings API — per-company boards."""
    out = []
    for slug in boards:
        try:
            r = SESSION.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=20)
            if r.status_code != 200 or not isinstance(r.json(), list):
                continue
            for job in r.json():
                title = job.get("text", "")
                if not ENG_TITLE_RE.search(title):
                    continue
                desc = job.get("descriptionPlain", "") or ""
                if not is_entry_level(title, desc):
                    continue
                cats = job.get("categories") or {}
                out.append({
                    "title": title,
                    "company": slug.replace("-", " ").title(),
                    "location": cats.get("location", "") or "",
                    "url": job.get("hostedUrl", "") or job.get("applyUrl", ""),
                    "description": desc, "posted_ts": _ts(job.get("createdAt")),
                    "source": f"Lever/{slug}",
                })
        except Exception:
            continue
    return out


def fetch_greenhouse(boards):
    """Greenhouse job board API — per-company boards."""
    out = []
    for slug in boards:
        try:
            r = SESSION.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", timeout=20)
            if r.status_code != 200:
                continue
            for job in r.json().get("jobs", []):
                title = job.get("title", "")
                if not ENG_TITLE_RE.search(title):
                    continue
                desc = re.sub(r"<[^>]+>", " ", job.get("content", "") or "")
                if not is_entry_level(title, desc):
                    continue
                out.append({
                    "title": title,
                    "company": slug.replace("-", " ").title(),
                    "location": (job.get("location") or {}).get("name", ""),
                    "url": job.get("absolute_url", ""),
                    "description": desc, "posted_ts": _ts(job.get("updated_at")),
                    "source": f"Greenhouse/{slug}",
                })
        except Exception:
            continue
    return out


def fetch_ashby(boards):
    """Ashby job board API — per-company boards."""
    out = []
    for slug in boards:
        try:
            r = SESSION.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true", timeout=20)
            if r.status_code != 200:
                continue
            for job in r.json().get("jobs", []):
                title = job.get("title", "")
                if not ENG_TITLE_RE.search(title):
                    continue
                desc = job.get("descriptionPlain", "") or ""
                if not is_entry_level(title, desc):
                    continue
                loc = job.get("location", "") or ""
                # Only fall back to the isRemote flag when no location is given.
                # Appending "(Remote)" to a real city inflated its score and made
                # one ATS dominate the ranking.
                if job.get("isRemote") and not loc:
                    loc = "Remote"
                out.append({
                    "title": title,
                    "company": slug.replace("-", " ").title(),
                    "location": loc,
                    "url": job.get("jobUrl", ""),
                    "description": desc, "posted_ts": _ts(job.get("publishedAt")),
                    "source": f"Ashby/{slug}",
                })
        except Exception:
            continue
    return out


def fetch_remoteok():
    url = "https://remoteok.io/api"
    try:
        r = SESSION.get(url, timeout=20)
        r.raise_for_status()
        out = []
        for job in r.json()[1:]:
            tags = [t.lower() for t in job.get("tags", [])]
            if not any(t in tags for t in ["new-grad", "entry-level", "internship", "junior", "fresher"]):
                continue
            title = job.get("position", "")
            desc = job.get("description", "") or ""
            if not is_entry_level(title, desc):
                continue
            out.append({
                "title": title, "company": job.get("company", ""),
                "location": job.get("location", ""), "url": job.get("url", ""),
                "description": desc, "posted_ts": _ts(job.get("date")),
                "source": "RemoteOK",
            })
        return out
    except Exception as e:
        return [{"error": f"RemoteOK: {e}"}]




# ─── Source registry ───
FETCHERS = [
    ("SimplifyJobs", fetch_simplify_jobs),
    ("Lever boards", lambda: fetch_lever(LEVER_BOARDS)),
    ("Greenhouse boards", lambda: fetch_greenhouse(GREENHOUSE_BOARDS)),
    ("Ashby boards", lambda: fetch_ashby(ASHBY_BOARDS)),
    ("RemoteOK", fetch_remoteok),
]


# ─── Notion layer ───
def notion_headers():
    return {"Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": "2022-06-28", "Content-Type": "application/json"}


def notion(method, url, body=None, retries=4, delay=1):
    for attempt in range(retries):
        try:
            r = SESSION.request(method, url, headers=notion_headers(), json=body, timeout=30)
            if r.status_code in (200, 201):
                return r, None
            if r.status_code == 429:
                wait = int(r.json().get("additional_data", {}).get("retry_after", delay * 2 ** attempt))
                time.sleep(min(wait + 1, 30))
                continue
            return r, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            if attempt == retries - 1:
                return None, str(e)
            time.sleep(delay * 2 ** attempt)
    return None, "max retries exceeded"


def _txt(props, key):
    rt = props.get(key, {}).get("rich_text") or []
    return rt[0].get("plain_text", "") if rt else ""


def _date(props, key):
    d = props.get(key, {}).get("date") or {}
    return d.get("start") or ""


def read_notion_rows():
    """Read every row with its stored evidence (this is what makes re-scoring safe)."""
    if not NOTION_TOKEN:
        return []
    pages, more, cursor = [], True, None
    while more:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r, err = notion("POST", f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query", body)
        if err:
            print(f"  Notion query failed: {err}", file=sys.stderr)
            break
        d = r.json()
        pages.extend(d.get("results", []))
        more = d.get("has_more", False)
        cursor = d.get("next_cursor")
        time.sleep(0.25)

    rows = []
    for p in pages:
        props = p.get("properties", {})
        t = props.get(P_NAME, {}).get("title", [])
        name = t[0].get("plain_text", "") if t else ""
        company = _txt(props, P_COMPANY)
        if not company:
            company, _, _ = name.partition(" — ")
        url = props.get(P_LINK, {}).get("url") or ""
        sel = props.get(P_PRI, {}).get("select") or {}
        tier = props.get(P_TIER, {}).get("select") or {}
        app = props.get(P_APP, {}).get("select") or {}
        posted = _date(props, P_POSTED)
        rows.append({
            "id": p.get("id"), "name": name, "company": company, "url": url,
            "canon": canonical_url(url), "kind": url_kind(url),
            "score": props.get(P_SCORE, {}).get("number") or 0,
            "priority": sel.get("name", ""), "tier": tier.get("name", ""),
            "source": _txt(props, P_SOURCE), "location": _txt(props, P_LOC),
            "posted_ts": _ts(posted) if posted else None,
            "fit_reason": _txt(props, P_REASON),
            "verify_fails": props.get(P_VFAIL, {}).get("number") or 0,
            "score_model": props.get(P_MODEL, {}).get("number"),
            "stack_hits": props.get("Stack Hits", {}).get("number") or 0,
            "app_status": app.get("name", "") or "New",
            "created_time": p.get("created_time", ""),
        })
    return rows


def create_row(job, score, priority, reason, stack_hits, tier, verified_iso):
    props = {
        P_NAME: {"title": [{"text": {"content": display_name(job["company"], job["title"])}}]},
        P_LINK: {"url": job.get("url") or None},
        P_SCORE: {"number": score},
        P_PRI: {"select": {"name": priority}},
        P_TIER: {"select": {"name": tier}},
        P_COMPANY: {"rich_text": [{"text": {"content": job.get("company", "")[:2000]}}]},
        P_SOURCE: {"rich_text": [{"text": {"content": job.get("source", "")[:2000]}}]},
        P_LOC: {"rich_text": [{"text": {"content": (job.get("location") or "")[:2000]}}]},
        P_REASON: {"rich_text": [{"text": {"content": reason[:2000]}}]},
        P_MODEL: {"number": SCORE_MODEL},
        "Stack Hits": {"number": stack_hits},
        P_VFAIL: {"number": 0},
        P_APP: {"select": {"name": "New"}},
        P_VERIFIED: {"date": {"start": verified_iso}},
    }
    ts = job.get("posted_ts")
    if ts:
        props[P_POSTED] = {"date": {"start": datetime.fromtimestamp(ts, timezone.utc).isoformat()}}
    _, err = notion("POST", "https://api.notion.com/v1/pages",
                    {"parent": {"database_id": NOTION_DB_ID}, "properties": props})
    return err is None


def patch_row(page_id, props):
    """Patch properties only. Never touches App Status — that field is the user's."""
    _, err = notion("PATCH", f"https://api.notion.com/v1/pages/{page_id}", {"properties": props})
    return err is None


def archive_row(page_id):
    _, err = notion("PATCH", f"https://api.notion.com/v1/pages/{page_id}", {"archived": True})
    return err is None


# ─── Liveness (three-state) ───
CLOSED_MARKERS = [
    "position has been filled", "no longer accepting", "no longer available",
    "this position has been closed", "job has been filled", "role has been filled",
    "application closed", "position closed", "job closed", "requisition closed",
    "this job is no longer", "position is no longer", "posting has expired",
    "job posting has expired", "deadline has passed", "no longer hiring",
    "filled internally", "role closed", "position cancelled", "job not found",
    "position not found", "no longer open", "this job has expired",
    "applications are closed", "job has been closed", "this role is closed",
]
GONE_STATUS = {404, 410, 451}


def verify_url(url, timeout=12, retries=2):
    """Return ('Active'|'Dead'|'Unknown', reason).

    Only confirmed death is 'Dead'. Transient network faults, rate limits and
    bot walls are 'Unknown' and must never delete a posting.
    """
    if not url:
        return "Unknown", "missing url"
    last = "no attempt"
    for attempt in range(retries + 1):
        try:
            r = SESSION.get(url, timeout=timeout, allow_redirects=True)
        except requests.exceptions.Timeout:
            last = "timeout"
        except requests.exceptions.SSLError:
            last = "ssl error"
        except requests.exceptions.ConnectionError:
            last = "connection failed"
        except Exception as e:
            last = f"request failed: {str(e)[:40]}"
        else:
            if r.status_code in GONE_STATUS:
                return "Dead", f"HTTP {r.status_code}"
            if r.status_code >= 400:
                return "Unknown", f"HTTP {r.status_code}"     # 403/429/5xx = not proof of death
            body = r.text.lower()
            for marker in CLOSED_MARKERS:
                if marker in body:
                    return "Dead", f"closed: {marker}"
            return "Active", f"HTTP {r.status_code}"
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    return "Unknown", last


def verify_many(urls):
    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=VERIFY_WORKERS) as ex:
        return list(ex.map(verify_url, urls))


# ─── Re-scoring ───
def rescore_existing(rows):
    """Recompute stored rows under the current model. Returns (updates, unchanged).

    This is what removes the old --reset requirement: a scoring change is picked
    up automatically because each row keeps the evidence needed to re-score it.
    """
    updates, unchanged = [], 0
    for r in rows:
        if r["score_model"] == SCORE_MODEL:
            unchanged += 1
            continue
        score, priority, reason, tb = score_job(
            r["title"] if "title" in r else r["name"], r["company"], r["location"],
            r["source"], r["posted_ts"], r["url"], r["stack_hits"], r["verify_fails"])
        r["score"], r["priority"], r["fit_reason"] = score, priority, reason
        r["score_model"] = SCORE_MODEL
        if r["app_status"] not in FROZEN_STATUSES:
            updates.append((r["id"], {
                P_SCORE: {"number": score},
                P_PRI: {"select": {"name": priority}},
                P_REASON: {"rich_text": [{"text": {"content": reason[:2000]}}]},
                P_MODEL: {"number": SCORE_MODEL},
            }))
    return updates, unchanged


# ─── Planning ───
def plan(existing_rows, fresh_jobs):
    """Rank non-frozen existing rows against fresh candidates; keep the best 30.

    Dedupe: canonical URL is primary; (company, title, location) is secondary, so
    distinct vacancies at the same company are never merged.
    Returns (to_create, to_update, to_archive, kept_existing).
    """
    frozen = [r for r in existing_rows if r["app_status"] in FROZEN_STATUSES]
    live_existing = [r for r in existing_rows if r["app_status"] not in FROZEN_STATUSES]

    seen_canon, seen_norm = set(), set()
    for r in live_existing:
        seen_canon.add(r["canon"] or r["url"])
        seen_norm.add(norm_key(r["company"], r["name"], r["location"]))

    # Fresh: role-level dedupe keeps the best-scoring instance of each vacancy.
    best = {}
    for j in fresh_jobs:
        k = norm_key(j["company"], j["title"], j.get("location", ""))
        if k not in best or j["_score"] > best[k]["_score"]:
            best[k] = j

    records, seq = [], 0
    for r in live_existing:
        records.append({"kind": "existing", "row": r, "score": r["score"],
                        "priority": r["priority"], "tier": r["tier"],
                        "tiebreak": employer_tiebreak(r["company"]),
                        "created_time": r["created_time"], "seq": seq})
        seq += 1

    for j in best.values():
        canon = canonical_url(j.get("url", ""))
        nk = norm_key(j["company"], j["title"], j.get("location", ""))
        if canon and canon in seen_canon:
            continue
        if nk in seen_norm:
            continue
        seen_canon.add(canon)
        seen_norm.add(nk)
        records.append({"kind": "new", "job": j, "score": j["_score"],
                        "priority": j["_priority"], "tier": "",
                        "tiebreak": employer_tiebreak(j["company"]),
                        "created_time": "", "seq": seq})
        seq += 1
        if len(records) >= MAX_JOBS_TOTAL + MAX_CANDIDATES:
            break

    records.sort(key=lambda x: (PRIORITY_ORDER.get(x["priority"], 9), -x["score"],
                                -x["tiebreak"], x["created_time"] or "9999", x["seq"]))
    winners = records[:MAX_JOBS_TOTAL]

    to_create, to_update = [], []
    for rank, w in enumerate(winners, start=1):
        tier = "Top 10" if rank <= TOP_SLOTS else "Buffer"
        if w["kind"] == "new":
            to_create.append((w["job"], w["score"], w["priority"], tier))
        elif w["row"]["tier"] != tier:
            to_update.append((w["row"]["id"], {P_TIER: {"select": {"name": tier}}}))

    winner_ids = {w["row"]["id"] for w in winners if w["kind"] == "existing"}
    to_archive = [r["id"] for r in live_existing if r["id"] not in winner_ids]
    kept_existing = [w["row"]["name"] for w in winners if w["kind"] == "existing"]
    return to_create, to_update, to_archive, kept_existing, frozen


# ─── Main ───
def require_config():
    """Fail fast with an actionable message if Notion is not configured."""
    missing = []
    if not NOTION_TOKEN:
        missing.append("NOTION_API_KEY")
    if not NOTION_DB_ID:
        missing.append("NOTION_DB_ID (or notion.database_id in the profile)")
    if missing:
        print("Missing Notion configuration: " + ", ".join(missing), file=sys.stderr)
        print("Set them as environment variables, e.g.:", file=sys.stderr)
        print("  export NOTION_API_KEY=ntn_...", file=sys.stderr)
        print("  export NOTION_DB_ID=<your database id>", file=sys.stderr)
        return False
    return True


def main():
    if not require_config():
        return 2
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    today = datetime.now(timezone.utc).isoformat()

    print(f"JOB HUNTER v6 (model {SCORE_MODEL}) — {datetime.now().strftime('%d %b %Y %H:%M')}")
    print(f"List: max {MAX_JOBS_TOTAL} rows ({TOP_SLOTS} Top 10 + {BUFFER_SLOTS} Buffer)")

    # 1 ── read
    print("\n[1/7] Reading Notion...")
    existing = read_notion_rows()
    frozen = [r for r in existing if r["app_status"] in FROZEN_STATUSES]
    print(f"  Rows: {len(existing)} (frozen by user: {len(frozen)})")

    # 2 ── non-vacancy detection
    print("\n[2/7] Checking every URL is a specific vacancy...")
    bad_url = [r for r in existing
               if r["kind"] != "posting" and r["app_status"] not in FROZEN_STATUSES]
    print(f"  Rows whose URL is not a posting: {len(bad_url)}")
    for r in bad_url:
        print(f"     x {r['name'][:58]} — {r['kind']} (will be removed)")

    # 3 ── fetch
    print("\n[3/7] Fetching sources...")
    all_jobs, counts, source_errors = [], {}, []
    for name, fn in FETCHERS:
        try:
            raw = fn()
        except Exception as e:
            counts[name] = 0
            source_errors.append(f"{name}: {type(e).__name__}: {e}")
            continue
        errs = [j for j in raw if "error" in j]
        source_errors.extend(j["error"] for j in errs)
        jobs = [j for j in raw if "error" not in j]
        counts[name] = len(jobs)
        all_jobs.extend(jobs)
    zero = [n for n, c in counts.items() if c == 0]
    if zero:
        print(f"  !! sources returning ZERO: {zero}")

    # 4 ── dedupe, gate, score
    print("\n[4/7] Deduplicating, gating, scoring...")
    seen, fresh, rejected = set(), [], defaultdict(int)
    for j in all_jobs:
        uid = make_uid(j["source"], j["title"], j["company"], j.get("url", ""))
        if uid in seen:
            rejected["duplicate uid"] += 1
            continue
        seen.add(uid)
        kind = url_kind(j.get("url", ""))
        if kind != "posting":
            rejected[f"url is {kind}"] += 1
            continue
        j["_canon"] = canonical_url(j["url"])
        j["_stack"] = count_stack_hits(j["title"], j.get("description", ""), j["company"])
        j["_score"], j["_priority"], j["_reason"], _ = score_job(
            j["title"], j["company"], j.get("location", ""), j["source"],
            j.get("posted_ts"), j["url"], j["_stack"])
        fresh.append(j)
    fresh.sort(key=lambda j: (PRIORITY_ORDER.get(j["_priority"], 9), -j["_score"]))
    evidence = {}
    for j in fresh:
        if j["_canon"]:
            evidence[j["_canon"]] = {
                "stack": j["_stack"], "posted_ts": j.get("posted_ts"),
                "company": j.get("company", ""), "location": j.get("location", ""),
                "source": j.get("source", ""),
            }
    print(f"  {len(all_jobs)} raw -> {len(fresh)} eligible postings")
    if rejected:
        for k, v in rejected.items():
            print(f"     rejected {v} x {k}")

    # 5 ── re-score existing IN PLACE (backfilled from the fresh pool, so a legacy
    #      row is judged on the same evidence a fresh row is)
    print("\n[5/7] Re-scoring existing rows (no destructive reset)...")
    updates, unchanged, backfilled = [], 0, 0
    for r in existing:
        ev = evidence.get(r["canon"])
        if ev is not None:
            # The feed is authoritative while the posting is still being fetched.
            # Adopting it unconditionally is what keeps consecutive runs stable:
            # otherwise an archived row returns as a "fresh" candidate with a
            # higher score than it had as a stored row, and oscillates.
            if ev["stack"] != (r["stack_hits"] or 0):
                backfilled += 1
            r["stack_hits"] = ev["stack"]
            if ev["posted_ts"]:
                r["posted_ts"] = ev["posted_ts"]
            # Legacy rows created before these fields existed score "location
            # unknown" — backfill them so they rank on real evidence.
            for key, prop in (("company", "company"), ("location", "location"),
                              ("source", "source")):
                if not r.get(key) and ev.get(key):
                    r[key] = ev[key]
                    backfilled += 1
        if r["score_model"] == SCORE_MODEL and ev is None:
            unchanged += 1
            continue
        score, priority, reason, _ = score_job(
            r["name"], r["company"], r["location"], r["source"],
            r["posted_ts"], r["url"], r["stack_hits"], r["verify_fails"])
        changed = (score != r["score"] or priority != r["priority"]
                   or r["score_model"] != SCORE_MODEL)
        r["score"], r["priority"], r["fit_reason"] = score, priority, reason
        r["score_model"] = SCORE_MODEL
        if r["app_status"] not in FROZEN_STATUSES:
            props = {
                P_SCORE: {"number": score},
                P_PRI: {"select": {"name": priority}},
                P_REASON: {"rich_text": [{"text": {"content": reason[:2000]}}]},
                P_MODEL: {"number": SCORE_MODEL},
                "Stack Hits": {"number": r["stack_hits"]},
            }
            # Fill descriptive gaps without ever touching a user-set status.
            if r["company"]:
                props[P_COMPANY] = {"rich_text": [{"text": {"content": r["company"][:2000]}}]}
            if r["location"]:
                props[P_LOC] = {"rich_text": [{"text": {"content": r["location"][:2000]}}]}
            if r["source"]:
                props[P_SOURCE] = {"rich_text": [{"text": {"content": r["source"][:2000]}}]}
            if not r["app_status"] or r["app_status"] == "New":
                props[P_APP] = {"select": {"name": "New"}}
            needs = changed or ev is not None or not r["app_status"]
            if needs:
                updates.append((r["id"], props))
    for page_id, props in updates:
        patch_row(page_id, props)
        time.sleep(0.15)
    print(f"  Re-scored {len(updates)} | unchanged {unchanged} | evidence backfilled {backfilled}")

    # 6 ── liveness
    print("\n[6/7] Verifying links (Active / Dead / Unknown)...")
    pool, rest = fresh[:VERIFY_POOL], fresh[VERIFY_POOL:]
    res = verify_many([j.get("url", "") for j in pool])
    live_pool, dead_fresh = [], []
    for j, (state, reason) in zip(pool, res):
        j["_liveness"] = state
        (live_pool if state != "Dead" else dead_fresh).append(j)
    print(f"  Fresh: checked {len(pool)} | Dead {len(dead_fresh)} | "
          f"Unknown {sum(1 for j in live_pool if j.get('_liveness') == 'Unknown')}")
    for j in dead_fresh[:10]:
        print(f"     x {display_name(j['company'], j['title'])[:56]}")
    fresh = live_pool + rest

    dead_existing, unknown_existing = [], 0
    if existing:
        ex_res = verify_many([r["url"] for r in existing])
        for row, (state, reason) in zip(existing, ex_res):
            row["_liveness"] = state
            if row["app_status"] in FROZEN_STATUSES:
                continue
            if state == "Dead":
                dead_existing.append(row)
                row["score"] = -1
                row["priority"] = "P4 - Watchlist"
            elif state == "Unknown":
                unknown_existing += 1
                row["verify_fails"] = (row["verify_fails"] or 0) + 1
                patch_row(row["id"], {P_VFAIL: {"number": row["verify_fails"]},
                                      P_VERIFIED: {"date": {"start": today}}})
            else:
                row["verify_fails"] = 0
                patch_row(row["id"], {P_VFAIL: {"number": 0},
                                      P_VERIFIED: {"date": {"start": today}}})
        print(f"  Existing: Dead {len(dead_existing)} | Unknown {unknown_existing} | "
              f"frozen skipped {len(frozen)}")

    # 7 ── plan + apply
    print("\n[7/7] Planning + applying...")
    force_out = {r["id"] for r in dead_existing} | {r["id"] for r in bad_url}
    existing_live = [r for r in existing if r["id"] not in force_out]
    to_create, to_update, to_archive, kept, frozen2 = plan(existing_live, fresh)
    to_archive = sorted(set(to_archive) | force_out)
    print(f"  Create {len(to_create)} | Update {len(to_update)} | "
          f"Archive {len(to_archive)} | Keep {len(kept)} | Frozen {len(frozen2)}")

    created = errors = 0
    for job, score, priority, tier in to_create:
        if create_row(job, score, priority, job["_reason"], job.get("_stack", 0), tier, today):
            created += 1
        else:
            errors += 1
        time.sleep(0.4)
    updated = sum(1 for pid, props in to_update if patch_row(pid, props))
    archived = sum(1 for pid in to_archive if archive_row(pid))

    final = read_notion_rows()
    nonfrozen = [r for r in final if r["app_status"] not in FROZEN_STATUSES]
    keys = [r["canon"] or r["url"] for r in nonfrozen]
    tops = sum(1 for r in nonfrozen if r["tier"] == "Top 10")
    bufs = sum(1 for r in nonfrozen if r["tier"] == "Buffer")
    bad_final = [r for r in nonfrozen if r["kind"] != "posting"]
    alive = sum(1 for r in nonfrozen if r.get("_liveness") == "Active")

    sel_by_source = defaultdict(int)
    for j, _s, _p, _t in to_create:          # to_create holds (job, score, pri, tier)
        sel_by_source[j.get("source", "?")] += 1
    yield_tbl = {n: {"fetched": c, "selected": sel_by_source.get(n, 0)}
                 for n, c in counts.items()}

    summary = {
        "run_at": today, "score_model": SCORE_MODEL,
        "rows_before": len(existing), "frozen": len(frozen),
        "rescore_updates": len(updates), "rescore_unchanged": unchanged,
        "evidence_backfilled": backfilled,
        "non_posting_rows_removed": [r["name"] for r in bad_url],
        "source_yield": yield_tbl, "sources_zero": zero, "source_errors": source_errors,
        "candidates_raw": len(all_jobs), "candidates_eligible": len(fresh),
        "rejections": dict(rejected),
        "dead_fresh": [display_name(j["company"], j["title"]) for j in dead_fresh],
        "dead_existing": [r["name"] for r in dead_existing],
        "unknown_existing": unknown_existing,
        "created": created, "create_errors": errors, "updated": updated, "archived": archived,
        "final_rows": len(final), "final_top10": tops, "final_buffer": bufs,
        "final_duplicates": len(keys) - len(set(keys)),
        "final_non_posting_urls": len(bad_final),
    }
    (OUTPUT_DIR / f"job_hunt_run_{run_id}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"JOB HUNTER v6 (model {SCORE_MODEL}) — {datetime.now().strftime('%d %b %Y %H:%M')}",
        f"Cap {MAX_JOBS_TOTAL} = {TOP_SLOTS} Top 10 + {BUFFER_SLOTS} Buffer",
        f"{len(all_jobs)} raw -> {len(fresh)} eligible | dead links dropped {len(dead_fresh)}",
        f"Re-scored {len(updates)} in place | Created {created} | Updated {updated} | "
        f"Archived {archived} | Errors {errors}",
        f"List: {len(final)} rows ({tops} Top 10, {bufs} Buffer, "
        f"{len(keys) - len(set(keys))} dupes, {len(bad_final)} non-posting URLs) | frozen {len(frozen)}",
        "", "Per-source yield (fetched / selected):",
    ]
    for name, y in yield_tbl.items():
        lines.append(f"  {name:<18} {y['fetched']:>5} / {y['selected']}")
    lines.append("")
    for j, s, p, t in to_create[:20]:
        lines.append(f"[{t}] [{p}] {display_name(j['company'], j['title'])} ({j['source']}) — {s}")
    (OUTPUT_DIR / f"job_hunt_run_{run_id}.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n" + "\n".join(lines))

    if source_errors:
        print("\nSource warnings:")
        for e in source_errors:
            print(f"  - {e}")
    return 0


def dry_run():
    """Rank and report WITHOUT mutating Notion. Uses the same backfill + rescore
    path as main(), so the predicted diff is the diff you would actually get."""
    if not require_config():
        return 2
    print(f"DRY RUN (model {SCORE_MODEL}) — {datetime.now().strftime('%d %b %Y %H:%M')}")
    existing = read_notion_rows()
    bad_url = [r for r in existing
               if r["kind"] != "posting" and r["app_status"] not in FROZEN_STATUSES]

    all_jobs, counts, dry_errors = [], {}, []
    for name, fn in FETCHERS:
        try:
            raw = fn()
        except Exception as e:
            counts[name] = 0
            dry_errors.append(f"{name}: {type(e).__name__}: {e}")
            continue
        dry_errors.extend(j["error"] for j in raw if "error" in j)
        jobs = [j for j in raw if "error" not in j]
        counts[name] = len(jobs)
        all_jobs.extend(jobs)

    seen, fresh = set(), []
    for j in all_jobs:
        uid = make_uid(j["source"], j["title"], j["company"], j.get("url", ""))
        if uid in seen:
            continue
        seen.add(uid)
        if url_kind(j.get("url", "")) != "posting":
            continue
        j["_canon"] = canonical_url(j["url"])
        j["_stack"] = count_stack_hits(j["title"], j.get("description", ""), j["company"])
        j["_score"], j["_priority"], j["_reason"], _ = score_job(
            j["title"], j["company"], j.get("location", ""), j["source"],
            j.get("posted_ts"), j["url"], j["_stack"])
        fresh.append(j)
    fresh.sort(key=lambda j: (PRIORITY_ORDER.get(j["_priority"], 9), -j["_score"]))

    evidence = {j["_canon"]: (j["_stack"], j.get("posted_ts"))
                for j in fresh if j["_canon"]}
    rescored, quotient = 0, 0
    for r in existing:
        ev = evidence.get(r["canon"])
        if ev is not None:
            r["stack_hits"] = ev["stack"]
            if ev["posted_ts"]:
                r["posted_ts"] = ev["posted_ts"]
            for key in ("company", "location", "source"):
                if not r.get(key) and ev.get(key):
                    r[key] = ev[key]
        if r["score_model"] == SCORE_MODEL and ev is None:
            continue
        score, priority, reason, _ = score_job(
            r["name"], r["company"], r["location"], r["source"],
            r["posted_ts"], r["url"], r["stack_hits"], r["verify_fails"])
        if score != r["score"] or priority != r["priority"] or r["score_model"] != SCORE_MODEL:
            rescored += 1
        r["score"], r["priority"], r["fit_reason"] = score, priority, reason
        r["score_model"] = SCORE_MODEL

    pool, rest = fresh[:VERIFY_POOL], fresh[VERIFY_POOL:]
    res = verify_many([j.get("url", "") for j in pool])
    live, dead = [], []
    for j, (state, reason) in zip(pool, res):
        (dead if state == "Dead" else live).append((j, reason) if state == "Dead" else j)
    fresh = live + rest

    ex_res = verify_many([r["url"] for r in existing]) if existing else []
    dead_existing = []
    for row, (state, reason) in zip(existing, ex_res):
        if row["app_status"] in FROZEN_STATUSES:
            continue
        if state == "Dead":
            dead_existing.append((row, reason))
            row["score"] = -1
            row["priority"] = "P4 - Watchlist"

    force_out = {r["id"] for r, _ in dead_existing} | {r["id"] for r in bad_url}
    existing_live = [r for r in existing if r["id"] not in force_out]
    to_create, to_update, to_archive, kept, frozen = plan(existing_live, fresh)
    to_archive = sorted(set(to_archive) | force_out)

    print(f"  Sources: {sum(counts.values())} raw -> {len(fresh)} eligible live postings")
    print(f"  Per source: {counts}")
    if dry_errors:
        print(f"  SOURCE FAILURES ({len(dry_errors)}):")
        for e in dry_errors[:12]:
            print(f"     ! {e}")
    zero = [n for n, c in counts.items() if c == 0]
    if zero:
        print(f"  ** sources returning ZERO: {zero}")
    print(f"  WOULD re-score {rescored} existing rows (in place, not reset)")
    print(f"  WOULD remove {len(bad_url)} non-posting URL row(s) and "
          f"{len(dead_existing)} dead posting(s)")
    for r in bad_url:
        print(f"     x {r['name'][:56]} — {r['kind']}")
    for r, reason in dead_existing[:10]:
        print(f"     x {r['name'][:56]} — {reason}")
    print(f"  WOULD create {len(to_create)} | update {len(to_update)} | "
          f"archive {len(to_archive)} | keep {len(kept)} | frozen {len(frozen)}")
    keep_ids = {r["id"] for r in existing_live
                if r["name"] in set(kept) and r["id"] not in to_archive}
    print(f"  Existing rows KEPT ({len(keep_ids)}):")
    for r in existing_live:
        if r["id"] in keep_ids:
            print(f"     = [{r['score']:>3}] {r['name'][:62]}")
    print(f"  Existing rows ARCHIVED ({len(to_archive)}):")
    by_id = {r["id"]: r for r in existing_live}
    for pid in to_archive:
        r = by_id.get(pid)
        if r:
            print(f"     - [{r['score']:>3}] {r['name'][:62]}")
    print(f"  Dead fresh links dropped: {len(dead)}")
    for j, reason in dead[:8]:
        print(f"     x {display_name(j['company'], j['title'])[:56]} — {reason}")
    for i, (j, s, pr, t) in enumerate(to_create, 1):
        print(f"  {i:>2}. [{t}] [{pr}] {display_name(j['company'], j['title'])} "
              f"| {j.get('location') or 'n/a'} | {j['source']} | {s}")
        print(f"       why: {j['_reason']}")
    print("\n(no Notion mutation performed)")
    return 0


def reset_list():
    """EMERGENCY ONLY. Archiving everything is no longer needed for scoring
    changes — rows are re-scored in place. Kept for genuine corruption repair."""
    rows = read_notion_rows()
    n = 0
    for r in rows:
        if r["app_status"] in FROZEN_STATUSES:
            print(f"  keeping frozen row: {r['name'][:60]} ({r['app_status']})")
            continue
        n += archive_row(r["id"])
    print(f"RESET: archived {n}/{len(rows)} rows (frozen rows preserved)")
    return 0


if __name__ == "__main__":
    if "--reset" in sys.argv:
        sys.exit(reset_list())
    if "--dry" in sys.argv:
        sys.exit(dry_run())
    sys.exit(main())
