"""Command-line demo. Shows what the assembler withholds.

    python -m posthog_context.cli "how do I capture a custom event in React?"

Prints the naive top-k result and the assembled block side by side, then the
assembled context itself. The comparison runs by default because the token
count on its own doesn't tell you anything; you need the number it's smaller
than.
"""

from __future__ import annotations

import argparse
import sys

from .assemble import how_do_i
from .ingest import estimate_tokens
from .retrieval import load

# ANSI, guarded so piping to a file stays readable.
_tty = sys.stdout.isatty()


def c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty else text


DIM, BOLD, RED, GREEN, BLUE, GREY = "2", "1", "31", "32", "34", "90"


def short(url: str) -> str:
    return url.replace("https://posthog.com/docs/", "")


def main() -> None:
    ap = argparse.ArgumentParser(description="PostHog docs context, assembled.")
    ap.add_argument("task", help="e.g. 'how do I capture a custom event in React?'")
    ap.add_argument("--budget", type=int, default=800, help="token budget (default 800)")
    ap.add_argument("-k", type=int, default=5, help="naive baseline top-k (default 5)")
    ap.add_argument("--quiet", action="store_true", help="skip the comparison header")
    args = ap.parse_args()

    index = load()
    naive = index.search(args.task, k=args.k)
    naive_tokens = estimate_tokens("\n\n".join(h.text for h in naive))
    naive_docs = list(dict.fromkeys(h.doc_path for h in naive))

    block = how_do_i(args.task, token_budget=args.budget)
    eng_docs = list(dict.fromkeys(short(s).split("#")[0] for s in block.sources))

    print()
    print(c(BOLD, f"  {args.task}"))
    print()

    if not args.quiet:
        pad = 16          # label column, so the token counts line up
        print(c(RED, f"  {'naive top-' + str(args.k):<{pad}}") +
              c(GREY, f"{naive_tokens:>6,} tokens   "
                      f"{len(naive)} chunks from {len(naive_docs)} docs"))
        for d in naive_docs:
            print(c(GREY, f"  {'':<{pad}}{d}"))
        print()
        saved = 1 - block.tokens / naive_tokens if naive_tokens else 0
        print(c(GREEN, f"  {'assembled':<{pad}}") +
              c(GREY, f"{block.tokens:>6,} tokens   "
                      f"{len(block.sources)} passages from {len(eng_docs)} doc"
                      f"{'s' if len(eng_docs) != 1 else ''}   ") +
              c(GREEN, f"{saved:.0%} smaller"))
        for d in eng_docs:
            print(c(BLUE, f"  {'':<{pad}}{d}"))
        if block.dropped:
            dropped = "  ".join(f"{k.replace('_', ' ')}: {v}"
                                for k, v in block.dropped.items())
            print()
            print(c(GREY, f"  {'withheld':<{pad}}{dropped}"))
        print()
        print(c(GREY, "  " + "─" * 68))
        print()

    print(block.markdown)
    print()


if __name__ == "__main__":
    main()
