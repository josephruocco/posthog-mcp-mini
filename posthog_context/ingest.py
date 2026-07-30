"""Ingest PostHog's JS/Web SDK docs into a local, heading-aware chunk index.

Docs-as-code: we read the markdown out of the `PostHog/posthog.com` repo rather
than scraping the rendered site. Two reasons that matter beyond taste. The
repo gives us frontmatter and heading structure the HTML has already thrown
away, and it gives us a stable source path per chunk, which is what lets every
returned passage carry a real posthog.com citation.

The non-obvious part is that PostHog's MDX is heavily componentized. A section
like "Capturing events" in `libraries/js/usage.mdx` is, on disk, an `import`
statement and `<WebSendEvents />` — the prose and the code sample live in a
`_snippets/` file somewhere else in the tree, which itself imports four more.
A loader that treats these files as plain markdown indexes empty sections for
exactly the content developers most need. So we resolve the import graph before
chunking.

Run:  python -m posthog_context.ingest
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path

import yaml

REPO_URL = "https://github.com/PostHog/posthog.com"
ROOT = Path(__file__).resolve().parent.parent
CLONE_DIR = ROOT / "data" / "posthog.com"
DOCS_DIR = CLONE_DIR / "contents" / "docs"
INDEX_PATH = ROOT / "data" / "index.json"

# One product area, done properly: the JS/Web SDK and client-side event capture.
# Everything here is either the SDK reference itself or the capture/identify/
# autocapture/install path a developer walks when instrumenting a web app.
# Deliberately absent: feature flags, session replay, experiments, and the
# fifteen server-side SDKs. Breadth is what makes a docs index vague.
SEED_DOCS = [
    "libraries/js/index.mdx",
    "libraries/js/usage.mdx",
    "libraries/js/config.mdx",
    "libraries/js/persistence.mdx",
    "libraries/js/types.mdx",
    "libraries/js/snippet-versioning.mdx",
    "libraries/react/index.mdx",
    "product-analytics/capture-events.mdx",
    "product-analytics/identify.mdx",
    "product-analytics/autocapture.mdx",
    "product-analytics/person-properties.mdx",
    "product-analytics/installation/index.mdx",
    # Deliberately absent: product-analytics/installation/{web,react}.mdx.
    # Both are one-line wrappers around React components that live in the
    # website's app code (`onboarding/product-analytics`), not in `contents/`.
    # There is no markdown to ingest — they yielded 21-token chunks of import
    # statement. Install is covered properly by libraries/js (both the HTML
    # snippet and the npm path) and libraries/react.
    "getting-started/install.mdx",
    "getting-started/send-events.mdx",
    "getting-started/identify-users.mdx",
]

# Several in-scope pages (capture-events.mdx especially) are multi-language tab
# groups that import one snippet per SDK — Python, Go, Java, Ruby, and a dozen
# others. Inlining those would put `posthog.capture()` in five syntaxes into a
# JS-focused index, and BM25 cannot tell them apart: a query about capturing an
# event matches the Ruby snippet nearly as well as the JS one. We drop non-web
# platform snippets at resolution time. This is a scope decision, not a
# retrieval trick — it is the ingest-time half of the same restraint the
# assembler applies at query time.
NON_WEB_PLATFORMS = {
    "python", "node", "nodejs", "php", "ruby", "go", "java", "rust",
    "flutter", "elixir", "dotnet", "android", "ios", "react-native",
    "kotlin", "swift", "api", "backend", "curl",
}

TOKENS_PER_CHAR = 0.25  # ~4 chars/token. See estimate_tokens().


def estimate_tokens(text: str) -> int:
    """Rough token count: ~4 characters per token.

    Deliberately not tiktoken. That is an extra dependency and the wrong
    tokenizer for whichever model is actually consuming this context. The
    budget in `how_do_i` is a guardrail against dumping 4,000 tokens of docs
    into an agent's window, and for that job a consistent estimate beats a
    precise one. Every call site goes through this function, so swapping in a
    real tokenizer later is a one-line change.
    """
    return int(len(text) * TOKENS_PER_CHAR) + 1


@dataclass
class Chunk:
    id: str
    doc_path: str          # e.g. "libraries/js/usage"
    doc_title: str
    heading: str
    heading_path: str      # "Capturing events > Custom event capture"
    level: int
    text: str
    source_url: str
    has_code: bool
    tokens: int


def clone_docs() -> None:
    """Sparse-clone the docs. Idempotent: a `git pull` if already present.

    Blobless + sparse keeps this to ~11MB of markdown instead of a multi-GB
    marketing site. We check out all of `contents/docs` (not just our seed
    paths) because snippet imports reach freely across the docs tree.
    """
    if (CLONE_DIR / ".git").exists():
        subprocess.run(["git", "-C", str(CLONE_DIR), "pull", "--quiet"], check=False)
        return
    CLONE_DIR.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth=1", "--filter=blob:none", "--sparse",
         REPO_URL, str(CLONE_DIR)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(CLONE_DIR), "sparse-checkout", "set", "contents/docs"],
        check=True,
    )


def split_frontmatter(raw: str) -> tuple[dict, str]:
    """Split a `---`-delimited YAML frontmatter block off the body."""
    if not raw.startswith("---"):
        return {}, raw
    end = raw.find("\n---", 3)
    if end == -1:
        return {}, raw
    try:
        meta = yaml.safe_load(raw[3:end]) or {}
    except yaml.YAMLError:
        meta = {}
    body = raw[end + 4:].lstrip("\n")
    return (meta if isinstance(meta, dict) else {}), body


IMPORT_RE = re.compile(
    r"^import\s+(?P<name>\w+)\s+from\s+['\"](?P<path>[^'\"]+)['\"]\s*;?\s*$",
    re.MULTILINE,
)

# Named imports — `import { WebsiteJSHtmlSnippet } from './x.tsx'`. These always
# point at .tsx React components rather than markdown, so there is nothing to
# inline; we just delete the line. Without this they survive into the index as
# literal text and BM25 happily matches queries against import paths.
NAMED_IMPORT_RE = re.compile(
    r"^import\s+\{[^}]*\}\s+from\s+['\"][^'\"]+['\"]\s*;?\s*$",
    re.MULTILINE,
)


def _is_non_web(snippet_path: str) -> bool:
    """True if a snippet is another SDK's version of the same content."""
    stem = Path(snippet_path).stem.lower()
    parts = re.split(r"[-_]", stem)
    # Match on trailing platform tokens: "send-events-python" -> python.
    # `react-native` is two tokens, so check the joined tail as well.
    tail2 = "-".join(parts[-2:])
    return parts[-1] in NON_WEB_PLATFORMS or tail2 in NON_WEB_PLATFORMS


def resolve_imports(path: Path, seen: frozenset[Path] = frozenset()) -> str:
    """Return a doc's body with `<Snippet />` components inlined, recursively.

    PostHog composes docs out of shared `_snippets/*.mdx` fragments so that one
    edit propagates to every SDK page. Great for them, hostile to a naive
    indexer. We follow each `import X from './y.mdx'` and substitute the
    expanded body of `y.mdx` wherever `<X />` appears.

    `seen` guards against an import cycle; a snippet that (transitively)
    includes itself would otherwise recurse forever.
    """
    if path in seen or not path.exists():
        return ""
    seen = seen | {path}

    _, body = split_frontmatter(path.read_text(encoding="utf-8"))

    # Map component name -> resolved file, and strip the import lines.
    resolved: dict[str, Path | None] = {}
    for m in IMPORT_RE.finditer(body):
        name, spec = m.group("name"), m.group("path")
        if not spec.endswith(".mdx"):
            resolved[name] = None       # a React component, e.g. 'components/Tab'
        elif _is_non_web(spec):
            resolved[name] = None       # another SDK's snippet — out of scope
        elif spec.startswith("."):
            resolved[name] = (path.parent / spec).resolve()
        else:
            resolved[name] = (DOCS_DIR / spec).resolve()
    body = NAMED_IMPORT_RE.sub("", IMPORT_RE.sub("", body))

    def substitute(m: re.Match) -> str:
        target = resolved.get(m.group("name"), None)
        if target is None:
            return ""
        return "\n" + resolve_imports(target, seen) + "\n"

    # Self-closing component usage: <WebSendEvents /> — possibly with props.
    return re.sub(
        r"<(?P<name>[A-Z]\w*)\b[^>]*/>",
        substitute,
        body,
    )


FENCE_RE = re.compile(r"(^```.*?^```)", re.MULTILINE | re.DOTALL)


def prose_only(text: str) -> str:
    """Everything outside fenced code blocks."""
    return "".join(seg for i, seg in enumerate(FENCE_RE.split(text)) if i % 2 == 0)


def strip_jsx(text: str) -> str:
    """Remove leftover JSX scaffolding, without touching fenced code.

    After import resolution there is still layout markup in the body —
    `<Tab.Group>`, `<List items={[...]} />`, `<CalloutBox>`. It carries no
    information for a retrieval index and its prop soup actively pollutes BM25
    term statistics. We strip tags but keep the prose between them.

    Splitting on fences first is the whole trick: JSX inside a ```jsx code
    block is the answer to "how do I capture an event in React", and a regex
    that strips tags blindly would eat the code sample we are here to serve.
    """
    out = []
    for i, seg in enumerate(FENCE_RE.split(text)):
        if i % 2 == 1:                       # odd segments are code fences
            out.append(seg)
            continue
        # Tab labels like <Tab>Web</Tab> are navigation, not content.
        seg = re.sub(r"<Tab\b[^>]*>.*?</Tab>", "", seg, flags=re.DOTALL)
        # Multi-line opening tags with prop objects: <List items={[ ... ]} />
        seg = re.sub(r"<[A-Z][\w.]*\b[^>]*?/?>", "", seg, flags=re.DOTALL)
        seg = re.sub(r"</[A-Z][\w.]*>", "", seg)
        # Raw HTML layout tags, which MDX allows inline. An explicit allowlist
        # rather than a blanket `<[a-z]...>` strip: PostHog's docs are full of
        # placeholders like <ph_project_token> and prose referencing <input>
        # and <textarea> tags, and eating those would corrupt real content.
        # This mattered — an unstripped <span class="..."> in a heading was
        # producing the anchor `#option-1-...-font-semibold-align-middle-...`.
        seg = re.sub(
            r"</?(?:span|div|br|hr|img|iframe|details|summary|p|blockquote|"
            r"figure|figcaption|picture|source|video)\b[^>]*>",
            "", seg, flags=re.DOTALL,
        )
        seg = re.sub(r"<!--.*?-->", "", seg, flags=re.DOTALL)
        out.append(seg)
    text = "".join(out)
    # Stripping multi-line JSX leaves behind ladders of indentation-only lines.
    # They are invisible in a diff and cost real tokens in a budgeted context
    # block, so flatten them before anything downstream counts tokens.
    text = "\n".join("" if not ln.strip() else ln.rstrip() for ln in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def slugify(heading: str) -> str:
    """GitHub-style anchor slug, so citations deep-link to the right section."""
    s = heading.lower()
    s = re.sub(r"`|\*|_", "", s)
    s = re.sub(r"[^\w\s-]", "", s)
    return re.sub(r"[\s]+", "-", s).strip("-")


def doc_url(doc_path: str) -> str:
    return f"https://posthog.com/docs/{doc_path}"


HEADING_RE = re.compile(r"^(#{2,4})\s+(.*?)\s*$")


def chunk_doc(rel_path: str) -> list[Chunk]:
    """Split one doc into heading-scoped chunks.

    Chunking by heading rather than by a fixed token window is the single
    highest-leverage choice in the whole ingest. A 512-token window splits a
    code fence from the sentence explaining it, and hands an agent half an
    example. A section is the unit the author already decided was one idea;
    we keep it whole.

    Content above the first `##` becomes an intro chunk — for short pages like
    `libraries/react/index.mdx` that preamble is the actual answer.
    """
    path = DOCS_DIR / rel_path
    meta, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    title = meta.get("title") or Path(rel_path).stem.replace("-", " ").title()

    doc_path = rel_path[: -len(".mdx")]
    if doc_path.endswith("/index"):
        doc_path = doc_path[: -len("/index")]

    body = strip_jsx(resolve_imports(path))

    # Walk the body, opening a new chunk at each h2/h3/h4 and tracking the
    # heading stack so a chunk knows its ancestry ("Capturing events > ...").
    chunks: list[Chunk] = []
    stack: dict[int, str] = {}
    current = {"heading": "Introduction", "level": 2, "lines": [], "path": title}
    in_fence = False

    def flush() -> None:
        text = "\n".join(current["lines"]).strip()
        if not text:
            return                      # heading with no body: nothing to index
        anchor = slugify(current["heading"])
        url = doc_url(doc_path) + (f"#{anchor}" if chunks or anchor != "introduction" else "")
        full = f"{current['heading']}\n\n{text}" if chunks else text
        chunks.append(Chunk(
            id=f"{doc_path}#{anchor}",
            doc_path=doc_path,
            doc_title=title,
            heading=current["heading"],
            heading_path=current["path"],
            level=current["level"],
            text=full,
            source_url=url,
            has_code="```" in text,
            tokens=estimate_tokens(full),
        ))

    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            flush()
            level, heading = len(m.group(1)), m.group(2)
            stack = {k: v for k, v in stack.items() if k < level}
            stack[level] = heading
            current = {
                "heading": heading,
                "level": level,
                "lines": [],
                "path": " > ".join([title] + [stack[k] for k in sorted(stack)]),
            }
        else:
            current["lines"].append(line)
    flush()
    return chunks


def check(chunks: list[Chunk]) -> None:
    """Fail loudly on the failure modes that produced garbage the first time.

    Every one of these fired on the first run. An ingest that silently emits
    21-token chunks of import statement is worse than one that crashes, because
    the index still *looks* populated and the damage only surfaces as
    mysteriously bad retrieval three milestones later.
    """
    assert chunks, "no chunks produced"

    empty = [c.id for c in chunks if len(c.text.strip()) < 40]
    assert not empty, f"near-empty chunks (unresolved component wrapper?): {empty[:5]}"

    # Only prose counts: `import posthog from 'posthog-js'` inside a fence is
    # the install instruction, not leaked MDX scaffolding.
    leaked = [c.id for c in chunks
              if re.search(r"^import\s+[\w{]", prose_only(c.text), re.MULTILINE)]
    assert not leaked, f"unstripped import statements: {leaked[:5]}"

    ladders = [c.id for c in chunks if re.search(r"\n[ \t]+\n", c.text)]
    assert not ladders, f"whitespace ladders from stripped JSX: {ladders[:5]}"

    html = [c.id for c in chunks if re.search(r"<span|<div|class=", c.heading)]
    assert not html, f"raw HTML leaked into headings: {html[:5]}"

    # The whole point of resolving the import graph: this doc's best content is
    # inlined from a _snippets file. If import resolution regresses, this is the
    # canary — the React capture example is the demo query for the entire repo.
    react = [c for c in chunks if "usePostHog" in c.text]
    assert react, "React `usePostHog` capture example missing — import resolution broken"

    dupe_ids = len(chunks) - len({c.id for c in chunks})
    assert dupe_ids == 0, f"{dupe_ids} duplicate chunk ids"


def main() -> None:
    clone_docs()
    all_chunks: list[Chunk] = []
    for rel in SEED_DOCS:
        cs = chunk_doc(rel)
        all_chunks.extend(cs)
        print(f"  {len(cs):3d} chunks  {rel}")

    check(all_chunks)

    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps([asdict(c) for c in all_chunks], indent=1))

    total_tokens = sum(c.tokens for c in all_chunks)
    with_code = sum(1 for c in all_chunks if c.has_code)
    print(f"\n{len(all_chunks)} chunks from {len(SEED_DOCS)} docs "
          f"({with_code} contain code, ~{total_tokens:,} tokens total)")
    print(f"wrote {INDEX_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
