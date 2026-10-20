---
name: agent-runtime
description: Use when configuring or debugging the LLM agent runtime around the job pipeline — provider routing, fallback chain, context budgets, delegation limits, or MCP tool servers.
version: 1
---

# Agent Runtime (routing, context, delegation)

This skill covers the **runtime layer** that sits around the deterministic
pipeline in `scripts/job_hunter.py`. The pipeline itself makes zero LLM
calls; the runtime is what schedules it, repairs it, reports on it, and
runs any agent-driven work alongside it.

Reference config: `config/agent-runtime.yaml` (redacted — no keys, no
machine-specific paths, no database ids).

## Provider routing and failover

- Ten providers are registered behind one OpenAI-compatible chat-completions
  surface. The agent never branches on which vendor is serving the turn.
- The primary endpoint is backed by a **16-model fallback chain across 9
  providers** (nvidia x2, orcarouter x4, kilo x2, nous x2, openrouter x2,
  groq, mistral, zai, tokenharbor). Ordered list lives in
  `fallback_providers`.
- **Never inline a secret.** Every provider references `key_env` — the name
  of the environment variable holding the key. A secret in config.yaml is a
  leak even in a private repo, because config.yaml is exactly the file
  people paste into issues.
- A rate-limited or dead provider must degrade the run, not abort it. If a
  change makes the runtime fail hard instead of falling through, that is a
  regression.

## Context engineering

Three independent mechanisms, each with a different failure mode:

1. **Progressive skill loading.** Only skill *metadata* (name + description)
   is resident in the prompt; the full `SKILL.md` body is fetched on demand.
   A skill library of any size therefore costs metadata, not context. If you
   see the prompt ballooning, check that a skill body is not being
   inlined eagerly.
2. **Hard memory budgets.** `memory_char_limit` (30K) and `user_char_limit`
   (~13.75K) are caps, not suggestions. Writes are atomic batches — one call
   that adds, patches, and removes — so a failed write never leaves a
   half-updated store. When the budget fills, replace or consolidate stale
   entries; never silently skip a save.
3. **Pre-LLM prompt hook.** The `pre_llm_call` hook rewrites
   underspecified prompts with a fast auxiliary model before the reasoning
   model sees them. It must **fail open** (`fail_closed: false`): on error
   or timeout the original prompt is used unchanged. A hook that blocks a
   run on a transient error is worse than no hook.

Auxiliary models (compression, vision, summarisation) run on a cheap tier
so the expensive reasoning model only sees work that needs it.

## Multi-agent delegation

- Children run in **isolated conversation contexts** with their own tool
  loops. A child knows nothing of the parent's conversation unless the task
  text passes it explicitly.
- `max_concurrent_children` bounds the batch; `max_spawn_depth` bounds the
  tree so a child cannot spawn an unbounded tree.
- Pin a child's `provider`/`model` when the child does cost-sensitive or
  bulk work. Unpinned children inherit the parent's model.
- `subagent_auto_approve` stays **false** by default. A child that hits a
  dangerous-command prompt must resolve non-interactively — blocking on
  stdin deadlocks a cron run, because the parent owns stdin.
- Pass the child everything it needs: paths, error text, constraints, and
  the required output shape. A child's final message is a self-report, not a
  verified fact — verify external side effects yourself.

## MCP tool servers

- Remote MCP servers use the standard MCP tool surface with per-server OAuth
  (`mcp.notion.com`, `mcp.supabase.com`). Prefer MCP over bespoke client
  code so there is one integration path.
- Enable a server deliberately; each one widens the tool surface the model
  can reach.
- A hosted connector and an MCP server with the same name are different
  things — the user's own words decide which one is meant.

## Scheduling

- Cron jobs run on a schedule; `catch_up_missed: true` means a machine that
  was off does not silently lose a day of tracking.
- Execution history is persisted, so a failed run is diagnosable after the
  fact rather than silently skipped.
- Scheduled runs deliver to a chat channel. Keep the delivery payload
  self-contained: the reader may not have the logs.

## Debugging order

1. Which provider actually served the turn? (fallback chain position)
2. Did the context get truncated, and by which mechanism?
3. Did a hook fire, and did it fail open?
4. Did the child agent inherit or get pinned to a model?
5. Is the MCP server enabled and authorised?

## License

MIT. Author: Priyanshu Raj ([@NeverSpot](https://github.com/NeverSpot)).
