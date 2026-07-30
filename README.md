# posthog-context-mcp

An MCP server that serves PostHog's JavaScript/Web SDK docs as **agent-optimized
context** — not a chunk dump — plus an eval harness that measures whether the
context it returns is actually good.

Status: work in progress. Build order is M0 → M5; see `NOTES.md`.

## The idea

A coding agent asks *"how do I capture a custom event in React with PostHog?"*
and gets back a tight, deduplicated, token-budgeted, citation-backed context
block. The interesting part isn't the retrieval — it's the assembly, and the
eval that proves the assembly beats a naive top-k baseline.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

More to come: ingest, server config snippet, eval results.
