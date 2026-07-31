"""MCP server exposing PostHog's JS/Web SDK docs to a coding agent.

Three tools, deliberately differentiated by what the agent is trying to do:

  search_posthog_docs  — "show me what exists"    (ranked snippets, no opinion)
  get_posthog_doc      — "give me the whole page" (escape hatch, full markdown)
  how_do_i             — "just tell me how"       (assembled, budgeted context)

The first two are plumbing. The third is the point — see assemble.py.

Run:  python -m posthog_context.server
"""

from __future__ import annotations

from mcp.server import MCPServer

from . import retrieval
from .assemble import how_do_i as assemble_context

server = MCPServer(
    name="posthog-context",
    version="0.1.0",
    instructions=(
        "PostHog JavaScript/Web SDK documentation, served as engineered context.\n\n"
        "Prefer `how_do_i` for implementation questions ('how do I capture a "
        "custom event in React?') — it returns a deduplicated, ordered, "
        "token-budgeted context block with citations, which is almost always "
        "what you want. Use `search_posthog_docs` to explore what exists, and "
        "`get_posthog_doc` only when you genuinely need a full page.\n\n"
        "Scope is the JS/Web SDK: installation, event capture, custom events, "
        "identify, person properties, autocapture, and configuration. This "
        "server does not cover feature flags, session replay, experiments, or "
        "the server-side SDKs."
    ),
)


@server.tool(
    description=(
        "Search PostHog's JS/Web SDK docs and return ranked snippets. Pure "
        "retrieval with no assembly — use this to discover what documentation "
        "exists. For 'how do I X' questions, use `how_do_i` instead."
    )
)
def search_posthog_docs(query: str, k: int = 5) -> list[dict]:
    """Ranked doc snippets for a query."""
    return [
        {
            "title": r.title,
            "heading": r.heading_path,
            "source_url": r.source_url,
            "snippet": r.snippet,
            "score": r.score,
        }
        for r in retrieval.load().search(query, k=k)
    ]


@server.tool(
    description=(
        "Fetch a full PostHog doc by path or slug, e.g. 'libraries/js/usage', "
        "'/docs/product-analytics/identify', or a full posthog.com URL. "
        "Returns the complete markdown — this can be large, so prefer "
        "`how_do_i` unless you need the whole page."
    )
)
def get_posthog_doc(path_or_slug: str) -> dict:
    """A whole doc, reassembled from its chunks in document order."""
    doc = retrieval.load().get_doc(path_or_slug)
    if doc is None:
        # An error dict rather than a raised exception: the agent can recover
        # by calling search, and telling it so beats a stack trace.
        return {
            "error": f"No doc matching {path_or_slug!r}.",
            "hint": "Try search_posthog_docs to find the right path.",
        }
    return doc


@server.tool(
    description=(
        "Answer a 'how do I X with PostHog?' task with an assembled context "
        "block: the minimal set of deduplicated, ordered, cited passages that "
        "answer it, within a token budget. Prefer this over search for any "
        "implementation question — it returns roughly a quarter the tokens of "
        "a raw top-k dump with materially higher precision. Pass the task in "
        "natural language, and mention the framework if you know it ('in "
        "React') so the right variant is selected."
    )
)
def how_do_i(task: str, token_budget: int = 800) -> dict:
    """Assembled, budgeted, cited context for a task."""
    block = assemble_context(task, token_budget=token_budget)
    return {
        "context": block.markdown,
        "sources": block.sources,
        "tokens": block.tokens,
        "token_budget": token_budget,
        # Surfaced so the agent can tell "one source because that's the whole
        # answer" apart from "one source because retrieval fell over".
        "filtered": block.dropped,
    }


def main() -> None:
    server.run()          # stdio transport by default


if __name__ == "__main__":
    main()
