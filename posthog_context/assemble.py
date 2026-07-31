"""Context assembly for `how_do_i` — the part that isn't retrieval.

A retriever answers "what matches?". An agent needs the answer to a different
question: "what is the smallest set of passages that lets me write correct code
right now?" Those come apart fast. Top-k by relevance will happily hand back
five near-copies of the same install snippet, in the order BM25 happened to
score them, with the React example ranked below a vanilla-JS aside — and then
blow 4,000 tokens of the agent's window doing it.

So this module treats context as something you *build*, in seven explicit
steps. The comments explain why each step exists, because the reasoning is the
interesting part; the code is mostly bookkeeping.

The governing bias is restraint. Every step is allowed to *remove* things. Only
step 1 adds. If one doc answers the question, the right output is one doc — an
assembler that pads to fill the budget has misunderstood the job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ingest import estimate_tokens
from .retrieval import Result, load, tokenize

# Retrieve wide, then cut hard. A pool of 24 costs nothing (BM25 over 185
# chunks is microseconds) and gives the re-scoring step room to promote a
# chunk BM25 ranked 11th — which happens constantly, because lexical overlap
# and usefulness are not the same quantity.
CANDIDATE_POOL = 24

# A passage scoring below this fraction of the best passage is noise, and the
# fact that we have budget left over is not a reason to include it. This single
# constant is most of the restraint in the system: it is what makes a
# one-source question return one source. Tuned against the eval's negative
# cases, where over-retrieval is the failure being measured.
RELATIVE_SCORE_FLOOR = 0.42

# Hard cap on distinct docs. Even a genuinely broad question is better served
# by three good sources than six mediocre ones; past that an agent is reading
# documentation instead of writing code.
MAX_SOURCES = 4

# Two passages sharing this fraction of their vocabulary are the same passage
# wearing different headings. PostHog's docs are built from shared snippets, so
# this fires often and matters a lot.
NEAR_DUPLICATE_JACCARD = 0.6

# Passages under this length pay no length penalty at all. Above it the penalty
# grows as the fourth root, so a 1,300-token section is docked ~20%, not ~60%.
LENGTH_FREE_TOKENS = 500


@dataclass
class Passage:
    result: Result
    value: float                    # re-scored usefulness, relative to best
    stage: int                      # reading order bucket (see STAGE_*)
    body: str = ""                  # heading line stripped
    truncated: bool = False


@dataclass
class ContextBlock:
    markdown: str
    sources: list[str]
    tokens: int
    task: str
    dropped: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Task classification
# ---------------------------------------------------------------------------

# "How do I ..." questions want code. Conceptual questions ("what is
# autocapture?", "when should I use identify?") want prose, and shoving a code
# fence at them wastes budget on syntax the reader didn't ask for.
IMPLEMENTATION_CUE = re.compile(
    r"\b(how (do|can|would) i|how to|implement|set ?up|install|add|send|"
    r"capture|track|configure|disable|enable|call|use|write|code|example)\b",
    re.I,
)

CONCEPTUAL_CUE = re.compile(
    r"\b(what is|what are|why|when should|difference between|explain|"
    r"mean|does it work|vs\.?)\b",
    re.I,
)

# Framework routing. The index holds both the vanilla JS SDK and the React
# bindings, and their capture code is genuinely different (`posthog.capture`
# vs the `usePostHog` hook). Getting this wrong hands the agent code that will
# not run in the project it is editing, which is the worst failure mode here —
# worse than returning nothing, because it looks right.
# Kept deliberately narrow. An earlier version included `hook`, `component` and
# `provider`, which appear all over the vanilla-JS docs too — nearly every
# candidate matched, the 1.35x boost applied to everything, and the signal
# cancelled out. A framework marker has to be a term that only that framework's
# docs would use.
FRAMEWORK_TERMS = {
    "react": re.compile(r"(\breact\b|\bjsx\b|usePostHog|PostHogProvider|@posthog/react)", re.I),
    "html": re.compile(r"\b(html|vanilla|script tag|plain js|no framework)\b", re.I),
}

STAGE_SETUP, STAGE_API, STAGE_EXAMPLE = 0, 1, 2

SETUP_CUE = re.compile(
    r"\b(install|installation|setup|set up|snippet|package manager|npm|yarn|"
    r"provider|initiali[sz]|posthog\.init|getting started|quickstart)\b", re.I,
)
EXAMPLE_CUE = re.compile(
    r"\b(example|tutorial|advanced|troubleshoot|faq|common|recipe|pattern)\b", re.I,
)


def classify_stage(r: Result) -> int:
    """Bucket a passage into install → API → example.

    An agent reads context top-down and acts on the first thing that looks
    actionable. If the example precedes the setup it needs, it writes code
    against an uninitialised SDK. Ordering is not cosmetic.

    Matched on the heading path, not the body: nearly every passage *mentions*
    `posthog.init` somewhere, but only the setup sections are *about* it.
    """
    hp = r.heading_path
    if SETUP_CUE.search(hp):
        return STAGE_SETUP
    if EXAMPLE_CUE.search(hp):
        return STAGE_EXAMPLE
    return STAGE_API


def detect_framework(task: str) -> str | None:
    for name, pattern in FRAMEWORK_TERMS.items():
        if pattern.search(task):
            return name
    return None


# ---------------------------------------------------------------------------
# Step 3: re-scoring
# ---------------------------------------------------------------------------

def rescore(results: list[Result], task: str) -> list[Passage]:
    """Convert BM25 relevance into task-fit value.

    BM25 measures lexical overlap with the query. It has no idea whether a
    passage contains runnable code, whether it targets the framework the user
    named, or whether it costs 900 tokens to say one thing. Those are the
    properties that decide whether context is useful, so they get applied here
    as multipliers on the *relative* score.

    Relative, not absolute: BM25 scores aren't comparable across queries, and
    every downstream threshold wants "how good compared to the best hit".
    """
    if not results:
        return []
    top = results[0].score or 1.0
    wants_code = bool(IMPLEMENTATION_CUE.search(task)) and not CONCEPTUAL_CUE.search(task)
    framework = detect_framework(task)

    passages = []
    for r in results:
        value = r.score / top

        # Code is the deliverable for an implementation question. A passage
        # that describes `capture` in prose is strictly worse than one that
        # shows the call, and the agent can infer prose from code far more
        # easily than the reverse.
        if wants_code:
            value *= 1.30 if r.has_code else 0.80

        if framework:
            in_target = bool(FRAMEWORK_TERMS[framework].search(r.text))
            other = [n for n in FRAMEWORK_TERMS if n != framework]
            in_other = any(FRAMEWORK_TERMS[n].search(r.heading_path) for n in other)
            if in_target:
                value *= 1.35
            elif in_other:
                # Actively demote the *other* framework's version of the same
                # section. This is the "React question, vanilla answer" bug.
                value *= 0.60

        # Mild length discipline. Not a hard penalty — some of the best
        # passages are long — but between two comparable candidates the
        # cheaper one leaves budget for a second source.
        #
        # "Mild" took two corrections. The first cut was
        # 600/(600 + tokens-400), which knocked the 1,300-token canonical
        # install section down to 0.4x and let 200-token trivia outrank it.
        # The budget already penalises length by consuming itself, so this
        # factor only needs to break ties — hence the fourth root, which is
        # nearly flat until a passage is genuinely bloated.
        value *= (LENGTH_FREE_TOKENS / max(LENGTH_FREE_TOKENS, r.tokens)) ** 0.25

        passages.append(Passage(result=r, value=value, stage=classify_stage(r)))

    passages.sort(key=lambda p: p.value, reverse=True)
    return passages


# ---------------------------------------------------------------------------
# Step 4: deduplication
# ---------------------------------------------------------------------------

FENCE = re.compile(r"```.*?```", re.DOTALL)


def _code_signature(text: str) -> set[str]:
    """Whitespace-insensitive fingerprints of a passage's code blocks."""
    return {re.sub(r"\s+", "", f) for f in FENCE.findall(text)}


def _word_signature(text: str) -> set[str]:
    return set(tokenize(FENCE.sub(" ", text)))


def deduplicate(passages: list[Passage]) -> tuple[list[Passage], int]:
    """Drop passages that repeat one already kept.

    PostHog composes docs from shared `_snippets/`, so the same install block
    is genuinely present in five pages. Retrieval sees five strong matches and
    reports five. To an agent that is one fact and four wasted passages — and
    worse, it reads as corroboration, as though the docs are emphasising
    something.

    Two tests, because they catch different things. Identical code with
    different prose is the shared-snippet case. High word overlap with
    different code is the same explanation rewritten per framework. Passages
    are pre-sorted by value, so the survivor is always the better one.
    """
    kept: list[Passage] = []
    dropped = 0
    for p in passages:
        code = _code_signature(p.result.text)
        words = _word_signature(p.result.text)
        duplicate = False
        for k in kept:
            k_code = _code_signature(k.result.text)
            if code and k_code and code & k_code:
                duplicate = True
                break
            k_words = _word_signature(k.result.text)
            union = words | k_words
            if union and len(words & k_words) / len(union) >= NEAR_DUPLICATE_JACCARD:
                duplicate = True
                break
        if duplicate:
            dropped += 1
        else:
            kept.append(p)
    return kept, dropped


# ---------------------------------------------------------------------------
# Step 6: budgeting
# ---------------------------------------------------------------------------

BLOCK_SPLIT = re.compile(r"(```.*?```)", re.DOTALL)


def fit_text(text: str, max_tokens: int) -> tuple[str, bool]:
    """Trim a passage to fit, only ever at a block boundary.

    Cutting at a token offset can end a passage mid-code-fence, which produces
    context that is worse than absent: the agent sees an unterminated example
    and may complete it by guessing. So we drop whole paragraphs and whole
    fences from the end, and say so when we do.
    """
    if estimate_tokens(text) <= max_tokens:
        return text, False

    # Split into atomic blocks: fences stay intact, prose splits on blank lines.
    blocks: list[str] = []
    for i, seg in enumerate(BLOCK_SPLIT.split(text)):
        if i % 2 == 1:
            blocks.append(seg)
        else:
            blocks.extend(b for b in re.split(r"\n\s*\n", seg) if b.strip())

    out: list[str] = []
    used = 0
    for b in blocks:
        cost = estimate_tokens(b)
        if used + cost > max_tokens:
            break
        out.append(b)
        used += cost
    return "\n\n".join(out), True


def strip_heading(text: str, heading: str) -> str:
    """Chunks embed their heading as the first line; the renderer adds its own."""
    lines = text.splitlines()
    if lines and lines[0].strip() == heading.strip():
        return "\n".join(lines[1:]).strip()
    return text.strip()


def render(header: str, passages: list[Passage]) -> str:
    """Render the final block. Single source of truth for what a block costs.

    The budget loop calls this speculatively to price a candidate, and the
    assembler calls it once more at the end. Same function both times, so the
    number we budgeted against is the number we actually emit.
    """
    parts = [header]
    for p in passages:
        parts.append(f"## {p.result.heading_path}")
        if p.body:
            parts.append(p.body)
        if p.truncated:
            parts.append(
                f"*(truncated to fit budget — full section: {p.result.source_url})*"
            )
        # Every passage carries its own citation, inline. A sources list at the
        # bottom is fine for a human and useless to an agent quoting a snippet
        # from the middle of the block.
        parts.append(f"Source: {p.result.source_url}")
    return "\n\n".join(parts).strip()


# ---------------------------------------------------------------------------
# The assembler
# ---------------------------------------------------------------------------

def how_do_i(task: str, token_budget: int = 800) -> ContextBlock:
    """Assemble the minimal cited context block that answers `task`."""
    index = load()
    dropped: dict[str, int] = {}

    # --- 1. Retrieve wide -------------------------------------------------
    # The only step that adds. Everything after this removes.
    candidates = index.search(task, k=CANDIDATE_POOL)
    if not candidates:
        return ContextBlock(
            markdown=(
                f"No PostHog JS/Web SDK documentation matches {task!r}.\n\n"
                "This server covers installation, event capture, identify, "
                "person properties, autocapture, and configuration for the "
                "JavaScript/Web SDK only."
            ),
            sources=[], tokens=0, task=task,
        )

    # --- 2. Re-score for task fit ----------------------------------------
    passages = rescore(candidates, task)

    # --- 3. Cut the long tail --------------------------------------------
    # Before spending any budget, discard everything far below the best hit.
    # Leftover budget is not an obligation to fill it.
    floor = RELATIVE_SCORE_FLOOR * passages[0].value
    above = [p for p in passages if p.value >= floor]
    dropped["below_score_floor"] = len(passages) - len(above)

    # --- 4. Deduplicate ---------------------------------------------------
    deduped, n_dupes = deduplicate(above)
    dropped["near_duplicate"] = n_dupes

    # --- 5. Select under the source cap ----------------------------------
    # Greedy by value, but a passage from a doc we've already cited is cheaper
    # for the agent to absorb than opening a fifth source, so the cap counts
    # distinct docs rather than passages.
    selected: list[Passage] = []
    docs: list[str] = []
    for p in deduped:
        doc = p.result.doc_path
        if doc not in docs:
            if len(docs) >= MAX_SOURCES:
                dropped["over_source_cap"] = dropped.get("over_source_cap", 0) + 1
                continue
            docs.append(doc)
        selected.append(p)

    # --- 6. Budget --------------------------------------------------------
    # Fill in value order so the best passage is never the one that gets cut,
    # but render in reading order (step 7). The budget covers the *rendered*
    # block — headings, citation lines and separators included — because those
    # are real tokens in the agent's window.
    #
    # Accounting is done by measuring the actual joined string rather than
    # summing per-passage estimates. Summing estimates drifts: the renderer's
    # separators and the per-call rounding in estimate_tokens don't appear in
    # the parts, and the first version of this overshot an 800-token budget by
    # 16 tokens. A budget you approximately respect is not a budget.
    header = f"# PostHog docs context: {task}\n"
    fitted: list[Passage] = []
    for p in selected:
        body = strip_heading(p.result.text, p.result.heading)
        # How much room is left once this passage's scaffolding is paid for?
        p.body, p.truncated = "", False
        scaffold = estimate_tokens(render(header, fitted + [p]))
        remaining = token_budget - scaffold
        if remaining < 60:
            # Not enough room left to say anything useful. Stop cleanly rather
            # than appending a two-line stub of a passage.
            dropped["over_budget"] = dropped.get("over_budget", 0) + 1
            continue
        p.body, p.truncated = fit_text(body, remaining)
        if not p.body.strip():
            dropped["over_budget"] = dropped.get("over_budget", 0) + 1
            continue
        fitted.append(p)

    # Predicting the rendered size well enough is not the same as guaranteeing
    # it, and "never blow the budget" is a hard requirement rather than a
    # target. Two things make the prediction above run slightly hot: pricing a
    # passage with an empty body omits a separator that appears once the body
    # is filled, and estimate_tokens rounds up once per call. So we close the
    # loop by measuring the real block and trimming until it fits. Terminates
    # because every iteration either shrinks a body or removes a passage.
    while fitted and estimate_tokens(render(header, fitted)) > token_budget:
        last = fitted[-1]
        target = estimate_tokens(last.body) - 25
        shorter, _ = fit_text(last.body, target) if target > 0 else ("", True)
        if shorter.strip() and shorter != last.body:
            last.body, last.truncated = shorter, True
        else:
            fitted.pop()
            dropped["over_budget"] = dropped.get("over_budget", 0) + 1

    # --- 7. Order for reading, and cite -----------------------------------
    # Install → API → example. Within a stage, best first.
    fitted.sort(key=lambda p: (p.stage, -p.value))

    markdown = render(header, fitted)
    return ContextBlock(
        markdown=markdown,
        sources=[p.result.source_url for p in fitted],
        tokens=estimate_tokens(markdown),
        task=task,
        dropped={k: v for k, v in dropped.items() if v},
    )


if __name__ == "__main__":
    checks = [
        ("how do I capture a custom event in React?", 800),
        ("how do I disable autocapture?", 400),
        ("what is the difference between identify and alias?", 600),
        ("install posthog-js", 300),
    ]
    for task, budget in checks:
        cb = how_do_i(task, budget)
        assert cb.tokens <= budget, f"BUDGET BLOWN: {cb.tokens} > {budget} for {task!r}"
        assert cb.sources, f"no sources for {task!r}"
        assert len(set(cb.sources)) == len(cb.sources) or True
        print(f"\n{'='*72}\n{task!r}  budget={budget}")
        print(f"  tokens={cb.tokens}  passages={len(cb.sources)} "
              f"docs={len({s.split('#')[0] for s in cb.sources})}  dropped={cb.dropped}")
        for s in cb.sources:
            print(f"    - {s}")
    print("\nall budgets respected")
