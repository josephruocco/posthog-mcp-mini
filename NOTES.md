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

## M1: what the ingest actually ran into

**PostHog's MDX is componentized, and this breaks naive loaders.** This was the
big surprise. The "Capturing events" section of `libraries/js/usage.mdx` is, on
disk, two lines:

```mdx
import WebSendEvents from '../../integrate/send-events/_snippets/send-events-web.mdx'
<WebSendEvents />
```

The prose and the code live in a shared `_snippets/` file — which itself imports
four more. Treat these as plain markdown and you index empty sections for
precisely the content developers need most. The ingest resolves the import graph
recursively (with a cycle guard) before chunking. `resolve_imports()` is the
least glamorous function in the repo and the one that makes the index worth
anything.

**Multi-language tab groups get filtered at ingest.** `capture-events.mdx` is a
16-SDK tab group importing one snippet per language. Inlining all of it puts
`posthog.capture()` in five syntaxes into a JS index, and BM25 can't tell them
apart — a query about capturing an event matches the Ruby snippet about as well
as the JS one. Non-web platform snippets are dropped at resolution time. This is
the ingest-time half of the same restraint `how_do_i` applies at query time.

**Two install pages had to be cut.** `product-analytics/installation/{web,react}.mdx`
are one-line wrappers around React components living in the website's app code
(`onboarding/product-analytics`), outside `contents/`. There is no markdown to
ingest — they produced 21-token chunks containing an import statement. Install
is covered properly by `libraries/js` (both the HTML snippet and the npm path)
and `libraries/react`. Verified that before cutting them.

**Strip HTML with an allowlist, never a blanket regex.** A `<span class="...">`
inside a heading was generating the anchor
`#option-1-...-bg-accent-text-gray-font-semibold-...`. But a blanket
`<[a-z][^>]*>` strip would eat `<ph_project_token>` placeholders and prose
referencing `<input>`/`<textarea>` tags. Explicit allowlist of layout tags only.

**The ingest asserts.** Six checks run before anything is written: no near-empty
chunks, no leaked imports, no whitespace ladders, no HTML in headings, no
duplicate ids, and a canary asserting the React `usePostHog` example survived
import resolution. Every one of them fired on the first run. An ingest that
silently emits junk is worse than one that crashes — the index still looks
populated and the damage surfaces as bad retrieval three milestones later.
(The leaked-import check needed its own fix: `import posthog from 'posthog-js'`
*inside a fence* is the install instruction, not scaffolding. It only inspects
prose now.)

## M2: retrieval

**Two BM25 fields, not one boosted field.** First implementation repeated
heading tokens into the body to boost them. That inflates document length, BM25
normalises by length, and short chunks then get rewarded twice — a 219-token
aside titled "Reset on logout" beat the canonical custom-event section for "how
do I capture a custom event in React". Heading and body are now separate
indexes, summed with `HEADING_WEIGHT = 1.6`. Also dropped `b` to 0.45: our
chunks are heading-scoped and fairly uniform, so the default 0.75 over-rewards
terse sections.

**Stemming is load-bearing.** Without it "capture a custom event" does not match
"Capturing custom events" — `capture`/`capturing` and `event`/`events` are
unrelated strings. The most important chunk in the index sat outside the top 24
for the project's own demo query. Added `snowballstemmer`: pure Python, zero
transitive dependencies. Chose a real stemmer over a hand-rolled suffix stripper
because the edge cases (property/properties, identify/identifying) are exactly
where suffix strippers fail.

**Query stopwords.** "how do I ..." contributes nothing, and `a` matches
constantly because the docs discuss the `<a>` tag.

## M3: the assembler

**Selection is by value; ordering is presentational.** Install → API → example
is applied to whatever survived selection, rather than forcing an install
passage into every answer. This matters for restraint: "how do I disable
autocapture" shouldn't get an install section just because the template wants
one.

**The budget is measured, not estimated.** Summing per-passage estimates drifted
— separators between rendered parts don't appear in the parts, and
`estimate_tokens` rounds up per call. The first version overshot an 800-token
budget by 16. Now a single `render()` prices candidates and emits the final
block, plus a closing loop that measures the real string and trims until it
fits. "Never blow the budget" is a hard requirement, not a target.

**Truncation only at block boundaries.** Cutting at a token offset can end a
passage mid-code-fence, which is worse than omitting it — the agent may complete
an unterminated example by guessing.

## M4: what the eval caught

Three configs, not two. The third — naive ranking truncated to the same
800-token ceiling — exists so the engineered config earns no credit merely for
being allowed to stop. It is genuinely smaller (461 vs 678 mean tokens) and pays
with a 79% hit rate.

Two assembler bugs that inspection had missed:

- **Length penalty was monotonic.** A 28-token section headed "Installation"
  reading `npm install @posthog/types` is a perfect lexical match for "how do I
  install posthog-js with npm" and tells the reader nothing. It beat the
  canonical 1,240-token install section. Usefulness is *concave* in length:
  long passages waste budget, short ones waste the request.
- **Framework routing was asymmetric.** It fired only when a framework was
  named. A neutral query got React's install section — correct code for a
  question nobody asked. Added the symmetric rule preferring framework-agnostic
  docs when no framework is mentioned.

**Overfitting risk, stated plainly.** Constants were tuned while watching 28
cases. Mitigation was to only make changes justifiable from principle rather
than from the score moving. A held-out set would be the honest next step.

## M5: the bug found by reading real output

Wiring `how_do_i` into the server and actually reading a tool response exposed
silent corruption nothing else caught. MDX `import` statements and the
JavaScript `import` statements *inside code samples* are the same syntax, and
`resolve_imports` ran over the whole body — deleting
`import { usePostHog } from '@posthog/react'` from inside the fence. The single
most important chunk was serving React code that cannot run. `resolve_imports`
now skips fenced code the same way `strip_jsx` always did, and the ingest
asserts the sample keeps its import.

Worth recording as a pattern: the fence-protection bug existed in `strip_jsx`
from the start and was fixed there, but the same hazard in `resolve_imports`
went unnoticed for three milestones because its output still *looked* like
valid markdown.
