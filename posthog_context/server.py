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


def main() -> None:
    server.run()          # stdio transport by default


if __name__ == "__main__":
    main()
