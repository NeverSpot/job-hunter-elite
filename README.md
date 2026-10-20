# Job Hunter Elite

A self-hosted, scheduled pipeline that discovers **entry-level / new-grad software
roles** and maintains a curated, ranked **30-slot tracker in Notion**.

Everything runs in plain Python. There are **no LLM calls in the pipeline** —
scoring is deterministic and rule-based, so a run costs zero tokens and produces
the same result for the same input.

```
+----------------------------------------------------------------------+
|  SOURCES (36 feeds)                                                  |
|  SimplifyJobs | RemoteOK | 5 Lever + 6 Greenhouse + 23 Ashby boards |
+----------------------------------+-----------------------------------+
                                   v
                        [1] ELIGIBILITY GATE
              (hard-reject 2+ yrs demand; require an
                   entry-level signal, else discard)
                                   v
                        [2] DEDUPE
        (canonical URL, then (company, title, location))
                                   v
                        [3] SCORING  (0-100, 6 factors)
             A role 30 | B grad-fit 20 | C location 20 |
             D stack 15 | E freshness 10 | F link-conf 5
                    minus an unverified-row penalty
                                   v
                   [4] PRIORITY BANDS  P1 / P2 / P3 / P4
                                   v
                        [5] LINK LIVENESS
        8 worker threads over a 90-URL pool ->
        Active / Dead / Unknown  (only Dead removes a row)
                                   v
                    [6] PLAN & UPSERT TO NOTION
        cap 30 rows = Top 10 + 20 Buffer; rows the user
        marked Applied/Rejected are frozen and never re-ranked
```

## What it actually does

1. **Eligibility gate first.** A posting is rejected outright if it explicitly
   demands 2+ years of experience, and it must carry a positive entry-level
   signal (new grad, entry level, junior, intern, fresher, graduate program,
   campus hire, …). Eligibility is decided *before* any scoring happens, and
   employer prestige is only ever a tie-breaker, never a ranking signal.
2. **Dedupe.** Primary key is the canonical posting URL (tracking params
   stripped). Secondary key is `(company, title, location)`, so two distinct
   vacancies at the same company are never merged.
3. **Six-factor weighted score, 0-100.** Explainable bands, capped so scores
   can't saturate:

   | Factor | Max | Meaning |
   |--------|-----|---------|
   | A. Target role | 30 | Title matches `target_roles`; else any engineering role |
   | B. Graduate / junior fit | 20 | new-grad/intern signal, else stated ≤1y, else unstated |
   | C. Location + work eligibility | 20 | India > worldwide remote > remote > other |
   | D. Stack overlap | 15 | hits against `professional.core_stack` (5 per hit) |
   | E. Freshness | 10 | ≤7d = 10, ≤30d = 7, older = 4, unknown = 3 |
   | F. Listing confidence | 5 | is the URL a specific posting, not a search page? |

   A row that fails liveness repeatedly is demoted by up to 8 points so it
   cannot hold a prime slot forever.
4. **Priority bands.** P1 Critical (≥85), P2 High (≥70), P3 Medium (≥55),
   P4 Watchlist (below that).
5. **Parallel liveness verification.** 8 worker threads check a 90-URL pool
   and classify each link as **Active**, **Dead**, or **Unknown**. Only
   confirmed death (404 / 410 / 451, DNS or connection failure, or a closure
   marker in the page body) removes a row. 401 / 403 / 429 / 5xx and timeouts
   are *Unknown* — a bot wall must never cost a real opening.
6. **Ranked Notion tracker, capped at 30 rows** (10 `Top 10` + 20 `Buffer`).
   Rows you mark `Applied` or `Rejected` are **frozen**: never re-ranked,
   never archived, never re-added. Every upsert stamps the current score
   model version, so a scoring change is picked up automatically without a
   destructive reset.

## Sources

**Aggregators (2)**

- **SimplifyJobs** — the `New-Grad-Positions` feed, filtered to active,
  engineering-category, Bachelor's-reachable listings posted in the last 90 days.
- **RemoteOK** — the public API, filtered to entry-level tags
  (`new-grad`, `entry-level`, `internship`, `junior`, `fresher`).

**Per-company ATS boards (34)**

- **Lever (5)** — `meesho`, `zeta`, `mindtickle`, `cred`, `fampay`
- **Greenhouse (6)** — `canonical`, `gitlab`, `thoughtworks`, `druva`, `slice`, `groww`
- **Ashby (23)** — `openai`, `harvey`, `sierra`, `ramp`, `cursor`, `baseten`,
  `langchain`, `vanta`, `temporal`, `replit`, `supabase`, `sardine`, `modal`,
  `hex`, `speak`, `linear`, `airbyte`, `posthog`, `railway`, `neon`, `runway`,
  `clerk`, `mux`

Generic "list all boards" ATS endpoints return 404, so each company is fetched
by slug (`api.lever.co/v0/postings/{slug}`, `boards-api.greenhouse.io/v1/boards/{slug}/jobs`,
`api.ashbyhq.com/posting-api/job-board/{slug}`).

## Scheduling

Runs **daily at 05:00** through a cron-style scheduler. The script itself is
pure Python with no LLM calls — zero tokens per run.

## Requirements

- Python 3.10+
- `PyYAML`
- `requests`

## How to run

```bash
# 1. Install dependencies
pip install PyYAML requests

# 2. Create your profile
cp profile.yaml.example profile.yaml
#    then edit profile.yaml (target_roles, core_stack, notion.database_id)

# 3. Export your Notion credentials
export NOTION_API_KEY="ntn_..."          # Notion integration token
export NOTION_DB_ID="<your database id>" # overrides notion.database_id in the profile

# 4. Run a cycle
python scripts/job_hunter.py
```

Useful flags:

```bash
python scripts/job_hunter.py --dry    # rank and report, mutate nothing in Notion
python scripts/job_hunter.py --reset  # emergency only: archive all non-frozen rows
```

### Environment variables

| Variable | Purpose |
|----------|---------|
| `NOTION_API_KEY` | Notion integration token. Required. |
| `NOTION_DB_ID` | Target database id. Falls back to `notion.database_id` in the profile. |
| `PROFILE_PATH` | Explicit path to `profile.yaml`. |
| `HERMES_HOME` | Base dir for `profiles/` and `cron/output/`. |
| `RESUME_PROJECT_DIR` | LaTeX resume project (used by `resume_tailor.py`). |
| `TECTONIC_BIN` | Path to the `tectonic` binary (falls back to `PATH`). |

## Repository layout

| Path | Purpose |
|------|---------|
| `scripts/job_hunter.py` | The pipeline: discovery → gate → score → verify → Notion upsert |
| `scripts/resume_tailor.py` | LaTeX resume tailoring helper (inject line → Tectonic compile) |
| `profile.yaml.example` | Template for `profile.yaml` (your own copy is gitignored) |
| `skills/career/job-hunter-elite/SKILL.md` | Agent-facing skill describing this pipeline |

## Target profile

- **User**: Priyanshu Raj, BTech CS, KIIT University (2026, CGPA 8.14)
- **Competitive programming**: Codeforces Expert (max 1825), CodeChef 5-Star
  (max 2027), IICPC Regionalist (56th in regionals, 276th in Prelims 2025),
  ICPC Regionalist (rank 42, Kanpur 2025) — 1800+ problems solved
- **Stack**: C++, TypeScript, Java, Go, Node.js/Express, MySQL/MongoDB/Redis,
  AWS, Linux, Nginx
- **Goal**: SDE / Backend / Applied AI role

## Notion schema

The target database needs these properties: `Name` (title), `Direct Link`
(url), `Score` (number), `Priority` (select: `P1 - Critical` / `P2 - High` /
`P3 - Medium` / `P4 - Watchlist`), `Tier` (select: `Top 10` / `Buffer`),
`Company`, `Source`, `Location`, `Posted Date`, `Last Verified`, `Fit Reason`,
`Verify Fails`, `Score Model`, `Stack Hits`, `App Status` (select:
`New` / `Applied` / `Rejected`).

## License

MIT — use freely for your own career automation.

**Author**: Priyanshu Raj ([@NeverSpot](https://github.com/NeverSpot))
