# posthog-context-mcp

An MCP server that hands PostHog's JavaScript SDK docs to a coding agent, plus
an eval that scores what comes back.

Ask it how to capture a custom event in React. It returns around 680 tokens of
deduplicated, cited passages. Wire a retriever straight to a tool and dump the
top five chunks, and the same question costs about 2,650 tokens. Both find a
correct doc every time.

![the CLI comparing a naive dump against the assembled block](demo.gif)

![naive vs engineered](eval/out/comparison.png)

| metric | naive top-5 | naive (budgeted) | **engineered** | better |
|---|---|---|---|---|
| Hit rate | 100% | 79% | **100%** | higher |
| Precision | 61% | 69% | **88%** | higher |
| Restraint (1-source cases) | 21% | 57% | **86%** | higher |
| Wrong sources per question | 1.14 | 0.25 | **0.29** | lower |
| Mean context tokens | 2,655 | 461 | **678** | lower |

28 hand-labelled questions. `naive top-5` concatenates the top five chunks.
`naive (budgeted)` takes that same ranking and stops at the 800-token ceiling,
so the engineered config gets no credit for being allowed to stop. Truncating
early costs it 21% of the answers.

Restraint is the metric worth explaining. Fourteen of the questions have exactly
one doc that answers them. On those, I score whether the system returned that
doc *alone*. Bringing a second source counts as a failure even when hit rate and
precision look fine. Any eval that only rewards finding the answer will tell you
a system returning the whole index is perfect.

## How it works

**Ingest** (`posthog_context/ingest.py`) sparse-clones `PostHog/posthog.com` and
reads the MDX. PostHog composes their docs from shared `_snippets/` fragments,
so a section like "Capturing events" is two lines on disk: an import and
`<WebSendEvents />`. The loader resolves that import graph recursively before it
chunks anything. Skip that step and you index empty sections where the useful
content should be.

**Retrieval** (`retrieval.py`) runs BM25 over two fields, heading and body,
scored separately and summed. Tokens get stemmed and camelCase gets split, so
`usePostHog` matches a query about posthog and "Capturing custom events" matches
someone asking how to capture a custom event. No vector database.

**Assembly** (`assemble.py`) is where the work is. Seven steps, and only the
first one adds anything: retrieve 24 candidates, re-score them for task fit, cut
everything below 42% of the top passage, fold near-duplicates together, cap the
answer at four distinct docs, fill the token budget in value order, then sort
into reading order with a citation on every passage.

**Server** (`server.py`) exposes three tools over stdio.

**Eval** (`eval/`) runs 28 cases through three configs and writes the chart.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

```bash
python -m posthog_context.ingest
```

The ingest pulls about 11MB of markdown into `data/`, which is gitignored, and
builds the index. PostHog's `/contents/` directory is MIT licensed and the rest
of that repo is not, so this reads only `/contents/` and vendors nothing.

## The tools

| tool | what it's for |
|---|---|
| `how_do_i(task, token_budget=800)` | The one that matters. Give it a task in plain language and it returns assembled, cited, budgeted context. |
| `search_posthog_docs(query, k=5)` | Ranked snippets with no assembly, for when an agent wants to see what documentation exists. |
| `get_posthog_doc(path_or_slug)` | A whole page, for when it genuinely needs all of it. |

## Connect it to an agent

Claude Desktop reads `~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS. Cursor reads `.cursor/mcp.json` in your project. Same shape either way:

```json
{
  "mcpServers": {
    "posthog-context": {
      "command": "/absolute/path/to/posthog-mcp-mini/.venv/bin/python",
      "args": ["-m", "posthog_context.server"],
      "cwd": "/absolute/path/to/posthog-mcp-mini"
    }
  }
}
```

Use absolute paths, and run the ingest first. The server refuses to start
without an index rather than quietly serving an empty one.

## Run the eval

```bash
python -m eval.run
```

It prints the table, names every case that failed, and writes
`eval/out/comparison.png`.

The loader asserts that the number of parsed cases matches the number the file
declares, and that every gold label points at a doc the index actually contains.
A loader that silently skips a malformed case still gives you a confident
number, just for a smaller experiment you didn't run, and nothing downstream can
tell you that happened.

Each module also checks itself:

```bash
python -m posthog_context.ingest      # 6 assertions on chunk quality
python -m posthog_context.retrieval   # ranking sanity
python -m posthog_context.assemble    # budgets are never exceeded
```

## Scope

The JavaScript and Web SDK, plus client-side capture. Installation, custom
events, identify, person properties, autocapture, configuration. 185 chunks from
15 docs.

Feature flags, session replay, experiments and the server-side SDKs are all left
out on purpose. Widening the index makes it vaguer, and the argument here is
about depth.

## What's still wrong with it

28 cases is a small eval, and I tuned constants while watching it. I kept myself
honest by only making changes I could argue from principle, so the length
penalty became concave because a 28-token stub can't answer anything, rather
than because 0.38 happened to score better. A held-out set is the next step.

The token counter estimates four characters per token. Every number here
inherits that approximation. It routes through one function, so swapping in a
real tokenizer is a one-line change.

Two restraint failures survive. Ask how to remove a stored super property and
you get the right passage plus a passage about removing person properties. Those
two read almost identically and they're different APIs. BM25 can't separate
them, and neither can my re-scoring. That specific problem is the honest case
for adding embeddings.

`NOTES.md` has the full decision log, including four bugs that produced output
looking completely fine.
