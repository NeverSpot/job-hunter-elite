---
name: job-hunter-elite
description: Use when managing the job search pipeline.
---

# Job Hunter Elite
This skill transforms the job search from a simple script into a high-leverage career engine.

## 🏗️ Pipeline Architecture
1. **Discovery (The Scout)**: Hybrid scraping (JobSpy + ATS Portals + Tier-1 Company List).
2. **Enrichment (The Analyst)**: Full JD extraction via Camofox $\rightarrow$ Semantic LLM Scoring (A-H Rubric).
3. **Tailoring (The Architect)**: LaTeX-based ATS-friendly resume generation with dynamic bullet rewriting.
4. **Tracking (The Strategist)**: Notion sync + Feedback-driven weight adjustment.

## 🚨 Critical Constraints
- **ATS-Friendliness**: Strictly use Space Grotesk/DM Sans or TeX Gyre Heros. No tables, no images, no complex columns.
- **The IICPC Mandate**: Every tailored resume **MUST** include exactly this line: 
  `• IICPC Regionalist with 56th rank in regionals and 276th rank in Prelims 2025.`
- **The Distinction Rule**: **IICPC $\neq$ ICPC**. These are two different contests. Never conflate them, never swap the names, and never assume one implies the other.

## 📊 Semantic Scoring Rubric (A-H)
- **A: Role Match**: Is the title a direct hit for SDE/Backend/Applied AI?
- **B: Tech Stack**: Overlap between core_stack (Node/TS/Go/AWS) and required tools.
- **C: Seniority**: Does the experience level align (0-2 years/New Grad)?
- **D: Company Tier**: Is it a Tier-1/High-Growth startup (e.g., OpenAI, Stripe)?
- **E: Location/Remote**: Matches preferred geography/Remote.
- **F: Legitimacy**: Is the job a repost or stale?
- **G: Work Auth**: Explicit sponsorship blockers?
- **H: Composite**: Final weighted average.

## 🛠️ Toolset
- `job_hunter.py`: Deterministic discovery and basic scoring.
- `resume_tailor.py`: LaTeX-based PDF generator.
- `Tectonic`: LaTeX compiler for ATS-perfect output.
