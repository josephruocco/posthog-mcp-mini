"""Naive vs engineered context retrieval, measured.

Run:  python -m eval.run

Three configurations answer the same 28 questions:

  naive_top5      top-5 chunks, dumped in BM25 order. What you get if you wire
                  a retriever to an MCP tool and call it done.
  naive_budgeted  the same ranking, concatenated until it hits the 800-token
                  budget. This config exists so the comparison is honest — it
                  denies the engineered config any credit it earns purely from
                  being allowed to stop.
  engineered      how_do_i(task, token_budget=800).

The metrics deliberately include ones that punish over-retrieval. Hit rate
alone is a broken objective: returning the entire index scores 100%. The
question worth answering is whether the context is *right and small*, so we
measure precision, wrong sources dragged in, and — on the cases labelled
single-source — whether the system had the discipline to return exactly one
doc when one doc was the whole answer.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from pathlib import Path

import yaml

from posthog_context.assemble import how_do_i
from posthog_context.ingest import estimate_tokens
from posthog_context.retrieval import load

CASES_PATH = Path(__file__).parent / "cases.yaml"
OUT_DIR = Path(__file__).parent / "out"
CHART_PATH = OUT_DIR / "comparison.png"

TOKEN_BUDGET = 800
NAIVE_K = 5


@dataclass
class Outcome:
    case_id: str
    sources: list[str]          # distinct doc_paths cited
    tokens: int
    hit: bool                   # at least one gold doc present
    precision: float            # share of cited docs that are gold
    extra: int                  # non-gold docs dragged in
    exact: bool | None          # single-source cases: exactly the one right doc


def load_cases() -> list[dict]:
    """Load ground truth, and refuse to run on a partial set.

    A loader that silently skips a malformed case doesn't produce a slightly
    worse number — it produces a confident number for a different, smaller
    experiment, and nothing downstream can tell. So we count the raw documents
    in the file and assert the parsed count matches, then assert every case has
    the keys the metrics depend on.
    """
    raw = CASES_PATH.read_text()
    cases = yaml.safe_load(raw)
    declared = raw.count("\n- id:") + raw.startswith("- id:")

    assert isinstance(cases, list), "cases.yaml must be a list"
    assert len(cases) == declared, (
        f"parsed {len(cases)} cases but the file declares {declared} — "
        "a case was silently dropped"
    )
    for i, c in enumerate(cases):
        missing = {"id", "question", "gold"} - set(c)
        assert not missing, f"case {i} ({c.get('id', '?')}) missing keys: {missing}"
        assert c["gold"], f"case {c['id']} has an empty gold list"

    ids = [c["id"] for c in cases]
    assert len(set(ids)) == len(ids), "duplicate case ids"

    index_docs = {ch["doc_path"] for ch in load().chunks}
    for c in cases:
        unknown = set(c["gold"]) - index_docs
        assert not unknown, (
            f"case {c['id']} labels docs that are not in the index: {unknown}. "
            "Either the label is wrong or the ingest dropped the doc."
        )

    print(f"loaded {len(cases)} cases from {CASES_PATH.name} "
          f"({sum(1 for c in cases if c.get('single_source'))} restraint cases)")
    return cases


def score(case: dict, sources: list[str], tokens: int) -> Outcome:
    gold = set(case["gold"])
    cited = list(dict.fromkeys(sources))       # distinct, order preserved
    correct = [s for s in cited if s in gold]
    return Outcome(
        case_id=case["id"],
        sources=cited,
        tokens=tokens,
        hit=bool(correct),
        precision=len(correct) / len(cited) if cited else 0.0,
        extra=len([s for s in cited if s not in gold]),
        exact=(len(cited) == 1 and cited[0] in gold)
        if case.get("single_source") else None,
    )


# --- the three configurations ----------------------------------------------

def run_naive_top5(case: dict) -> Outcome:
    hits = load().search(case["question"], k=NAIVE_K)
    # A raw dump is what it says: every chunk's full text, concatenated.
    body = "\n\n".join(h.text for h in hits)
    return score(case, [h.doc_path for h in hits], estimate_tokens(body))


def run_naive_budgeted(case: dict) -> Outcome:
    """Same ranking, but stop at the budget. The fair baseline."""
    hits = load().search(case["question"], k=NAIVE_K)
    used, kept, texts = 0, [], []
    for h in hits:
        cost = estimate_tokens(h.text)
        if used + cost > TOKEN_BUDGET:
            break
        used += cost
        kept.append(h.doc_path)
        texts.append(h.text)
    return score(case, kept, estimate_tokens("\n\n".join(texts)))


def run_engineered(case: dict) -> Outcome:
    block = how_do_i(case["question"], token_budget=TOKEN_BUDGET)
    doc_paths = [
        url.replace("https://posthog.com/docs/", "").split("#")[0]
        for url in block.sources
    ]
    return score(case, doc_paths, block.tokens)


CONFIGS = {
    "naive_top5": run_naive_top5,
    "naive_budgeted": run_naive_budgeted,
    "engineered": run_engineered,
}


# --- aggregation ------------------------------------------------------------

def summarize(outcomes: list[Outcome]) -> dict:
    exacts = [o.exact for o in outcomes if o.exact is not None]
    return {
        "hit_rate": statistics.mean(o.hit for o in outcomes),
        "precision": statistics.mean(o.precision for o in outcomes),
        "extra_sources": statistics.mean(o.extra for o in outcomes),
        "restraint": statistics.mean(exacts) if exacts else 0.0,
        "tokens": statistics.mean(o.tokens for o in outcomes),
    }


ROWS = [
    ("hit_rate", "Hit rate", "{:.0%}", "higher"),
    ("precision", "Precision", "{:.0%}", "higher"),
    ("restraint", "Restraint", "{:.0%}", "higher"),
    ("extra_sources", "Extra sources", "{:.2f}", "lower"),
    ("tokens", "Mean tokens", "{:,.0f}", "lower"),
]


def print_table(results: dict[str, dict]) -> None:
    names = list(CONFIGS)
    width = max(len(label) for _, label, _, _ in ROWS) + 2
    head = f"{'metric':<{width}}" + "".join(f"{n:>17}" for n in names) + "   better"
    print("\n" + head)
    print("-" * len(head))
    for key, label, fmt, direction in ROWS:
        line = f"{label:<{width}}"
        for n in names:
            line += f"{fmt.format(results[n][key]):>17}"
        print(line + f"   {direction}")

    base, eng = results["naive_budgeted"], results["engineered"]
    dump = results["naive_top5"]
    print(f"\nengineered vs naive_budgeted (same {TOKEN_BUDGET}-token ceiling):")
    print(f"  precision      {base['precision']:.0%} -> {eng['precision']:.0%}")
    print(f"  restraint      {base['restraint']:.0%} -> {eng['restraint']:.0%}")
    print(f"  extra sources  {base['extra_sources']:.2f} -> {eng['extra_sources']:.2f}")
    # Stated plainly and in the unflattering direction where it is unflattering.
    # The budgeted baseline is *smaller* than the engineered block, because it
    # truncates the moment the next chunk doesn't fit — which is also why it
    # misses answers the engineered config finds. Cheap and wrong is not a win,
    # but pretending it isn't cheaper would be dishonest.
    ratio = eng["tokens"] / base["tokens"]
    print(f"  mean tokens    {base['tokens']:,.0f} -> {eng['tokens']:,.0f} "
          f"({ratio:.2f}x — the budgeted baseline is smaller because it "
          f"truncates early, at the cost of hit rate)")
    print(f"\nengineered vs naive_top5 (the realistic naive implementation):")
    print(f"  precision      {dump['precision']:.0%} -> {eng['precision']:.0%}")
    print(f"  restraint      {dump['restraint']:.0%} -> {eng['restraint']:.0%}")
    print(f"  extra sources  {dump['extra_sources']:.2f} -> {eng['extra_sources']:.2f}")
    print(f"  mean tokens    {dump['tokens']:,.0f} -> {eng['tokens']:,.0f} "
          f"({1 - eng['tokens'] / dump['tokens']:.0%} smaller)")


def make_chart(results: dict[str, dict]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    names = list(CONFIGS)
    colors = ["#c8bfb2", "#9b9086", "#1d4aff"]     # engineered last, in blue
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.4))

    quality = [("hit_rate", "Hit rate"), ("precision", "Precision"),
               ("restraint", "Restraint\n(1-source cases)")]
    x = range(len(quality))
    w = 0.26
    ax = axes[0]
    for i, n in enumerate(names):
        ax.bar([v + i * w for v in x], [results[n][k] for k, _ in quality],
               w, label=n, color=colors[i])
    ax.set_xticks([v + w for v in x])
    ax.set_xticklabels([lbl for _, lbl in quality], fontsize=9)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("score")
    ax.set_title("Quality — higher is better", fontsize=11)
    ax.legend(fontsize=8, frameon=False)

    ax = axes[1]
    ax.bar(names, [results[n]["extra_sources"] for n in names], color=colors)
    ax.set_title("Wrong sources dragged in — lower is better", fontsize=11)
    ax.set_ylabel("mean per question")
    ax.tick_params(axis="x", labelsize=8)

    ax = axes[2]
    vals = [results[n]["tokens"] for n in names]
    ax.bar(names, vals, color=colors)
    ax.axhline(TOKEN_BUDGET, ls="--", lw=1, color="#666")
    ax.text(0.02, TOKEN_BUDGET, f" budget {TOKEN_BUDGET}", va="bottom",
            fontsize=8, color="#666", transform=ax.get_yaxis_transform())
    ax.set_title("Context size — lower is better", fontsize=11)
    ax.set_ylabel("mean tokens")
    ax.tick_params(axis="x", labelsize=8)

    fig.suptitle("PostHog docs context: naive retrieval vs engineered assembly",
                 fontsize=13)
    fig.tight_layout()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(CHART_PATH, dpi=150)
    print(f"\nchart -> {CHART_PATH.relative_to(Path(__file__).parent.parent)}")


def main() -> None:
    cases = load_cases()
    results, per_case = {}, {}
    for name, fn in CONFIGS.items():
        outcomes = [fn(c) for c in cases]
        assert len(outcomes) == len(cases), "an outcome went missing"
        results[name] = summarize(outcomes)
        per_case[name] = {o.case_id: o for o in outcomes}

    print_table(results)

    # Name the failures. An eval that only reports aggregates hides the cases
    # that would tell you what to fix next.
    misses = [c["id"] for c in cases if not per_case["engineered"][c["id"]].hit]
    if misses:
        print(f"\nengineered misses ({len(misses)}):")
        for cid in misses:
            got = per_case["engineered"][cid].sources
            gold = next(c["gold"] for c in cases if c["id"] == cid)
            print(f"  {cid}\n      got  {got}\n      want {gold}")

    sloppy = [
        c["id"] for c in cases
        if c.get("single_source") and per_case["engineered"][c["id"]].exact is False
    ]
    if sloppy:
        print(f"\nrestraint failures — right answer, too many sources ({len(sloppy)}):")
        for cid in sloppy:
            print(f"  {cid}: {per_case['engineered'][cid].sources}")

    make_chart(results)


if __name__ == "__main__":
    main()
