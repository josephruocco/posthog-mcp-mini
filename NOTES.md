# Notes

Running log of decisions and things I had to verify rather than assume.

## Verified

**Docs repo & path.** `github.com/PostHog/posthog.com`, docs live at
`contents/docs/**` as `.md`/`.mdx`. Confirmed via the GitHub contents API before
writing the loader. My product area maps to:

- `contents/docs/libraries/js/` — `index`, `usage` (33KB), `config` (46KB),
  `persistence`, `types`, `snippet-versioning`
- `contents/docs/product-analytics/` — `capture-events`, `identify`,
  `autocapture`, `installation/`

**License.** The repo's `LICENSE` is split. Everything outside `/contents/` is
all-rights-reserved ("please do not duplicate ... our website"). Everything
*inside* `/contents/` is MIT. We only read `/contents/`, and we don't vendor it
into this repo — the clone and the built index are gitignored, the ingest script
is committed. That's the reproducible-and-respectful combination.

**MCP SDK.** `mcp` 2.0.0 on PyPI, requires Python >=3.10. Major version bump, so
I introspected the installed package instead of trusting memory — and it's a
good thing I did. **`FastMCP` no longer exists in 2.0.** `mcp.server.fastmcp` is
gone; the decorator-style server class is now `MCPServer`:

```python
from mcp.server import MCPServer          # not mcp.server.fastmcp.FastMCP
server = MCPServer(name="posthog-context")
@server.tool()                            # same decorator ergonomics
def search_posthog_docs(query: str, k: int = 5) -> list[dict]: ...
server.run()                              # transport="stdio" is the default
```

Every blog post and LLM completion written before this release will hand you the
`FastMCP` import, and it will `ModuleNotFoundError`. Verified signatures:
`MCPServer.__init__(name, title, description, instructions, version, ...)`,
`.tool(name, title, description, annotations, structured_output, ...)`,
`.run(transport: Literal["stdio","sse","streamable-http"] = "stdio")`.

**Environment.** Python 3.11.5, no `uv` on this machine → plain `venv` + `pip`.

## Decisions

**No vector DB, BM25 only.** The brief's thesis is that the *assembly* is the
differentiator, not the retrieval. BM25 over heading-aware chunks is a strong
enough floor that any eval delta is attributable to assembly, which is exactly
the comparison I want to be able to make.

**Chunk by heading, not token window.** A fixed window splits a code fence from
the sentence that explains it. The unit of useful context here is a doc section.
