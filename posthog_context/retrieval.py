"""BM25 retrieval over the chunk index.

Lexical search, no vector database. That is a deliberate choice, not a
limitation I'm apologising for. Developer questions about an SDK are unusually
literal — people search for `capture`, `identify`, `posthog.init`,
`autocapture`, `person_profiles`. These are exactly the rare, high-IDF terms
BM25 is best at, and exactly the terms embeddings blur together (`capture` and
`identify` are neighbours in embedding space and opposites in an API).

It also keeps the experiment honest. The thesis of this repo is that assembly
beats retrieval volume. Holding retrieval fixed and cheap means any delta the
eval shows between the naive baseline and `how_do_i` is attributable to the
assembly step, which is the claim I actually want to make.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import snowballstemmer
from rank_bm25 import BM25Okapi

# Pure-Python, zero transitive dependencies. A real stemmer rather than a
# hand-rolled suffix stripper because the edge cases are where suffix strippers
# embarrass themselves: property/properties, identify/identifying.
_STEMMER = snowballstemmer.stemmer("english")

INDEX_PATH = Path(__file__).resolve().parent.parent / "data" / "index.json"

# Split camelCase so `usePostHog` also matches a query for "posthog", and
# `capture_pageview` matches "pageview". SDK identifiers are compound words;
# a tokenizer that treats them as opaque strings throws away most of the signal.
CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Headings are a strong prior — a section literally titled "Custom event
# capture" is a better answer to "how do I capture a custom event" than a
# passing mention buried in prose.
#
# The obvious implementation is to repeat heading tokens into the body and
# index once. Don't. Repeating tokens inflates the document *length*, and BM25
# normalises by length, so short chunks get boosted twice — once for the
# repetition and again for being short. In practice a 219-token aside called
# "Reset on logout" outscored the canonical custom-event section for the query
# "how do I capture a custom event in React". Two fields, scored separately and
# summed, keeps heading evidence out of the body's length statistics.
HEADING_WEIGHT = 1.6

# BM25's `b` controls how hard short documents are rewarded. The default 0.75
# is tuned for web pages of wildly varying length; our chunks are already
# heading-scoped and fairly uniform, and aggressive normalisation just
# surfaces terse asides over substantive sections.
BM25_B = 0.45

# Query-side stopwords. "how do I ..." is how developers phrase questions and
# contributes nothing but noise — `a` in particular matches PostHog's docs
# constantly, because they discuss the `<a>` tag.
STOPWORDS = frozenset("""
a an the do does did how what when where which who why is are was were be been
being i you it its this that these those to of in on at for with from by as or
and if then than my me we our your can could should would will shall may might
""".split())


@dataclass
class Result:
    id: str
    title: str
    heading: str
    heading_path: str
    source_url: str
    snippet: str
    score: float
    doc_path: str
    has_code: bool
    tokens: int
    text: str


def tokenize(text: str) -> list[str]:
    """Lowercase, stemmed word tokens, with compound identifiers split apart.

    `capture_pageview` yields ["capture_pageview", "capture", "pageview"] —
    keeping the whole token *and* its parts, so an exact query for the config
    key still outranks a doc that merely discusses pageviews.

    Stemming is not optional here, which I learned the hard way. Without it,
    the query "capture a custom event" does not match a section headed
    "Capturing custom events": `capture`/`capturing` and `event`/`events` are
    unrelated strings to BM25. The single most important chunk in the whole
    index was falling outside the top 24 candidates for the repo's own demo
    query. Docs headings are written in the gerund and the plural; developer
    questions are written in the infinitive and the singular. Something has to
    bridge that, and a stemmer is the cheapest thing that does.
    """
    text = CAMEL_BOUNDARY.sub(" ", text)
    tokens: list[str] = []
    for tok in re.findall(r"[a-z0-9_]+", text.lower()):
        tokens.append(tok)
        if "_" in tok:
            tokens.extend(part for part in tok.split("_") if part)
    return _STEMMER.stemWords(tokens)


class DocIndex:
    def __init__(self, chunks: list[dict]) -> None:
        self.chunks = chunks
        # Two fields, two indexes. See HEADING_WEIGHT for why this isn't just
        # heading tokens concatenated into the body.
        self.bm25_body = BM25Okapi([tokenize(c["text"]) for c in chunks], b=BM25_B)
        self.bm25_head = BM25Okapi(
            [tokenize(c["heading_path"]) for c in chunks], b=BM25_B
        )

    def score(self, query: str):
        q = [t for t in tokenize(query) if t not in STOPWORDS]
        if not q:                       # query was nothing but stopwords
            q = tokenize(query)
        return self.bm25_body.get_scores(q) + HEADING_WEIGHT * self.bm25_head.get_scores(q)

    def search(self, query: str, k: int = 5) -> list[Result]:
        scores = self.score(query)
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        results = []
        for i in ranked[:k]:
            if scores[i] <= 0:
                break          # no lexical overlap at all; padding to k would lie
            c = self.chunks[i]
            results.append(Result(
                id=c["id"],
                title=c["doc_title"],
                heading=c["heading"],
                heading_path=c["heading_path"],
                source_url=c["source_url"],
                snippet=make_snippet(c["text"], query),
                score=round(float(scores[i]), 3),
                doc_path=c["doc_path"],
                has_code=c["has_code"],
                tokens=c["tokens"],
                text=c["text"],
            ))
        return results

    def get_doc(self, path_or_slug: str) -> dict | None:
        """Fetch a whole doc, reassembled from its chunks in document order.

        Accepts what an agent might plausibly pass: a bare path
        (`libraries/js/usage`), a site path (`/docs/libraries/js/usage`), a full
        URL, or just the last segment (`usage`). Being liberal here costs eight
        lines and saves the agent a retry.
        """
        want = path_or_slug.strip().rstrip("/")
        want = re.sub(r"^https?://(www\.)?posthog\.com", "", want)
        want = want.removeprefix("/docs/").removeprefix("docs/").lstrip("/")
        want = want.split("#")[0]

        paths = {c["doc_path"] for c in self.chunks}
        match = None
        if want in paths:
            match = want
        else:
            tail = [p for p in paths if p.split("/")[-1] == want or p.endswith("/" + want)]
            if len(tail) == 1:
                match = tail[0]

        if match is None:
            return None
        parts = [c for c in self.chunks if c["doc_path"] == match]
        return {
            "doc_path": match,
            "title": parts[0]["doc_title"],
            "source_url": f"https://posthog.com/docs/{match}",
            "markdown": "\n\n".join(c["text"] for c in parts),
            "tokens": sum(c["tokens"] for c in parts),
            "sections": [c["heading"] for c in parts],
        }


def make_snippet(text: str, query: str, width: int = 320) -> str:
    """A window around the first query-term hit, not a blind head-slice.

    A snippet's job is to let the agent decide whether to spend tokens on the
    full chunk. Truncating from the top usually shows the heading and the first
    sentence of preamble — the least informative part of the section.
    """
    terms = {t for t in tokenize(query) if len(t) > 2}
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if terms & set(tokenize(line)):
            start = max(0, i - 1)
            break
    window = "\n".join(lines[start:]).strip()
    if len(window) <= width:
        return window
    return window[:width].rsplit(" ", 1)[0] + "…"


@lru_cache(maxsize=1)
def load() -> DocIndex:
    """Load and build the index once per process."""
    if not INDEX_PATH.exists():
        raise FileNotFoundError(
            f"No index at {INDEX_PATH}. Run: python -m posthog_context.ingest"
        )
    return DocIndex(json.loads(INDEX_PATH.read_text()))


if __name__ == "__main__":
    ix = load()
    # Retrieval sanity check: each query must surface its obvious home doc.
    expectations = [
        ("capture a custom event in react", "libraries/react"),
        ("disable autocapture", "product-analytics/autocapture"),
        ("identify a user", "product-analytics/identify"),
        ("install posthog-js with npm", "libraries/js"),
    ]
    for q, expected in expectations:
        hits = ix.search(q, k=5)
        paths = [h.doc_path for h in hits]
        assert expected in paths, f"{q!r} -> {paths}, expected {expected}"
        print(f"ok  {q!r}\n    {hits[0].heading_path}  ({hits[0].score})")

    assert ix.get_doc("libraries/js/usage")["tokens"] > 1000
    assert ix.get_doc("/docs/libraries/react")["title"] == "React"
    assert ix.get_doc("https://posthog.com/docs/product-analytics/identify") is not None
    assert ix.get_doc("nonsense-doc") is None
    print("\nget_doc ok")
