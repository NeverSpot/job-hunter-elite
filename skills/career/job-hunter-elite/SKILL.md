---
name: job-hunter-elite
description: Use when running or debugging the deterministic job-discovery pipeline that maintains the ranked 30-slot Notion tracker.
version: 6
---

# Job Hunter Elite (v6 — deterministic pipeline)

## What this is

A scheduled, self-hosted pipeline that discovers **entry-level / new-grad
software roles** and maintains a curated, ranked **30-slot tracker in Notion**.

**Zero LLM calls.** Scoring is deterministic and rule-based. Do not describe
this pipeline as AI-scored or LLM-scored.

- Script: `scripts/job_hunter.py` (v6, score model 6)
- Schedule: **daily at 05:00** via a cron-style scheduler (NOT every 4 hours)
- Cap: 30 rows = 10 `Top 10` + 20 `Buffer`

## Pipeline stages (match the code order exactly)

1. **Eligibility gate FIRST.** Reject any posting that explicitly demands 2+
   years of experience. Require a positive entry-level signal (new grad, entry
   level, junior, intern, fresher, graduate program, campus hire, …).
   Eligibility is decided before scoring; employer prestige is only a
   tie-breaker, never a ranking signal.
2. **Dedupe.** Canonical URL first, then `(company, title, location)`. Two
   distinct vacancies at one company are never merged.
3. **Scoring — 0-100, six capped factors:** A. Target role 0/15/30 ·
   B. Graduate-or-junior fit 0/8/12/20 · C. Location + work eligibility
   0/4/5/14/18/20 · D. Stack overlap 5 per hit (cap 15) · E. Freshness
   3/4/7/10 · F. Listing confidence 0/2/5 (is the URL a real posting?).
   Minus an unverified-row penalty (up to 8 pts) for repeatedly unverifiable
   rows, so they cannot hold a prime slot forever.
4. **Priority bands:** P1 Critical ≥85, P2 High ≥70, P3 Medium ≥55,
   P4 Watchlist below.
5. **Liveness:** 8 worker threads over a 90-URL pool → **Active / Dead /
   Unknown**. Only *confirmed* death removes a row.
6. **Plan + Notion upsert:** keep the best 30; rows the user marked
   `Applied` / `Rejected` are frozen and never re-ranked, archived, or
   re-added. Every upsert stamps the current score-model version.

## Sources — 34 per-company ATS boards + 2 aggregators

- **Aggregators:** SimplifyJobs (`New-Grad-Positions`), RemoteOK (entry-level
  tag filter).
- **Lever (5):** meesho, zeta, mindtickle, cred, fampay
- **Greenhouse (6):** canonical, gitlab, thoughtworks, druva, slice, groww
- **Ashby (23):** openai, harvey, sierra, ramp, cursor, baseten, langchain,
  vanta, temporal, replit, supabase, sardine, modal, hex, speak, linear,
  airbyte, posthog, railway, neon, runway, clerk, mux

Generic "list all boards" ATS endpoints 404 — always fetch per-company slugs:
`api.lever.co/v0/postings/{slug}`, `boards-api.greenhouse.io/v1/boards/{slug}/jobs`,
`api.ashbyhq.com/posting-api/job-board/{slug}`.

**Not a source:** LinkedIn, Indeed, Glassdoor, Naukri, Internshala, Wellfound,
Jobright, or the `vanshb03/New-Grad-Jobs-India` repo. v6 does not scrape them.

## Configuration

- `NOTION_API_KEY` — Notion integration token (environment variable).
- `NOTION_DB_ID` — target database id (environment variable). Falls back to
  `notion.database_id` in `profile.yaml`. Nothing is hardcoded in the script.
- `PROFILE_PATH`, `HERMES_HOME` — optional overrides.

## Operational rules (learned the hard way)

- **Only definite death removes a row.** 404/410/451, DNS/connection failure,
  or a closure marker in the body = Dead. 401/403/429/5xx/timeout = Unknown →
  keep the row, but bump its verify-fail counter.
- **Word-boundary keyword matching.** `"intern" in text` also matches
  "internal" — compile with `\b`, list `intern` / `internship` separately.
- **Curated feeds are entry-level by source.** SimplifyJobs listings carry an
  empty `description`, so gate those on source reputation and only reject
  explicit experience demands.
- **Never synthesise a location string.** Use the raw location; fall back to
  `"Remote"` only when the field is empty. Appending `"(Remote)"` to a real
  city inflates its score.
- **"Remote in USA" is not workable from India** — score it low. Reserve the
  top band for India, worldwide/anywhere/global, or remote with no US-only token.
- **Employer prestige never enters the score.** It is a 0–2 tie-breaker only.
- **No destructive reset for scoring changes.** Rows are re-scored in place
  from stored evidence; `--reset` is emergency-only corruption repair.
- **Rate limiting:** 0.5 s between `POST /v1/pages` calls; retry on 429 using
  `additional_data.retry_after`.
- **Notion ids:** the id in the page URL is the `database_id` (use as `parent`
  when creating pages); queries need the `data_source_id`. Every target
  database must be shared with the Notion integration or it 404s.
- **Runtime budget:** keep a run under 180 s. Verify only the top ~90 ranked
  URLs with `ThreadPoolExecutor(max_workers=8)` — a serial health check over
  the whole DB is what caused a 3600 s timeout.

## Notion schema

`Name` (title), `Direct Link` (url), `Score` (number), `Priority` (select),
`Tier` (select: `Top 10` / `Buffer`), `Company`, `Source`, `Location`,
`Posted Date`, `Last Verified`, `Fit Reason`, `Verify Fails`, `Score Model`,
`Stack Hits`, `App Status` (select: `New` / `Applied` / `Rejected`).

## Commands

```bash
python scripts/job_hunter.py          # full cycle
python scripts/job_hunter.py --dry    # rank + report, no Notion mutation
python scripts/job_hunter.py --reset  # emergency: archive non-frozen rows
```

## Resume tailoring (separate script)

`scripts/resume_tailor.py` injects the achievement line and compiles with
Tectonic. It needs `RESUME_PROJECT_DIR` and `TECTONIC_BIN` set, or a
`tectonic` on `PATH`. **Hard constraints:** ATS-safe font (TeX Gyre Heros or
Space Grotesk/DM Sans), no tables or images, and always include
`• IICPC Regionalist with 56th rank in regionals and 276th rank in Prelims 2025.`
**IICPC ≠ ICPC** — two different contests, never conflated.

## License

MIT. Author: Priyanshu Raj ([@NeverSpot](https://github.com/NeverSpot)).
