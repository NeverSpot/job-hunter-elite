# Job Hunter Elite — AI-Powered Career Pipeline

An elite, production-grade job hunting pipeline designed for **high-leverage career automation**. Built for Hermes Agent with multi-stage architecture: Discovery → Enrichment → Semantic Scoring → ATS-Optimized Tailoring → Feedback Loop.

## 🏗️ Architecture

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│   SCOUT     │───▶│  ANALYST    │───▶│  SCORER     │───▶│  ARCHITECT  │───▶│ STRATEGIST  │
│ Discovery   │    │ Enrichment  │    │ A-H Rubric  │    │  LaTeX PDF  │    │ Notion +    │
└─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘
```

### Stages

1. **Scout (Discovery)**: Hybrid scraping — JobSpy (LinkedIn, Indeed, Glassdoor) + ATS portals (Greenhouse, Lever, Ashby) + Tier-1 company watchlist
2. **Analyst (Enrichment)**: Full JD extraction via Camofox browser → structured parsing
3. **Scorer (Semantic)**: 8-dimension A-H rubric (Role, Stack, Seniority, Company Tier, Location, Legitimacy, Work Auth, Composite)
4. **Architect (Tailoring)**: Dynamic LaTeX resume generation with bullet rewriting → Tectonic compilation → ATS-perfect PDF
5. **Strategist (Tracking)**: Notion sync + rejection pattern analysis → adaptive weight adjustment

## 🚨 Critical Constraints (Non-Negotiable)

- **ATS-Friendliness**: TeX Gyre Heros font with `Kerning=Off` — zero split-token artifacts (TypeScript→T ypeScript, CGPA→CGP A)
- **IICPC Mandate**: Every tailored resume **MUST** include: `• IICPC Regionalist with 56th rank in regionals and 276th rank in Prelims 2025.`
- **Distinction Rule**: **IICPC ≠ ICPC** — two separate contests, never conflated
- **New-Grad Filter**: Hard-block on ≥3 years experience requirements

## 🛠️ Components

| Component | Path | Purpose |
|-----------|------|---------|
| Master Skill | `skills/career/job-hunter-elite/SKILL.md` | Hermes agent orchestration logic |
| Discovery Engine | `scripts/job_hunter.py` | Multi-source fetch, eligibility filter, basic scoring, Notion push |
| Tailor Engine | `scripts/resume_tailor.py` | LaTeX injection → Tectonic compilation → PDF |
| Profile | `profile.yaml` | User profile (skills, target roles, graduation year, etc.) |

## 📋 Requirements

- Python 3.12+ with: `PyYAML`, `requests`, `urllib3`
- Tectonic 0.17.0+ (LaTeX engine)
- Hermes Agent runtime
- Notion API key (for tracking)
- Camofox browser (for JD enrichment)

## 🚀 Quick Start

```bash
# 1. Install dependencies
pip install PyYAML requests

# 2. Configure profile.yaml with your details
cp profile.yaml.example profile.yaml

# 3. Run discovery cycle
python scripts/job_hunter.py

# 4. Tailor resume for a specific JD
python scripts/resume_tailor.py "Paste job description here"
```

## 📦 Deployment

- **Cron**: Runs every 4 hours via Hermes cron (`0 */4 * * *`)
- **Delivery**: Results pushed to Notion "Verified Job Tracker" + Telegram
- **Feedback Loop**: Mark jobs in Notion → next cycle adjusts scoring weights

## 🎯 Target Profile

- **User**: Priyanshu Raj, BTech CS KIIT (2026, CGPA 8.14)
- **CP**: Codeforces Expert (1825), CodeChef 5-Star (2027), IICPC Regionalist (56th), ICPC Regionalist (42nd Kanpur)
- **Stack**: C++, TypeScript, Java, Go, Node.js/Express, MySQL/MongoDB/Redis, AWS, Linux, Nginx
- **Goal**: ₹30LPA+ SDE/Backend/Applied AI role → ₹1 Cr CTC in 3 years

## 📚 Influences

Merged best practices from:
- Career-Ops (A-H rubric, human-in-the-loop, ATS PDF)
- JobPilot AI (LangGraph multi-agent, TF-IDF + LLM hybrid scoring)
- ai-job-agent (adaptive pattern learning, feedback-driven weights)
- Deviad/job-hunter (Pi Agent skills, salary enrichment)
- Remotivated/job-hunt-skills (modular skill architecture)

## 🔒 License

MIT — Use freely for your own career automation.
