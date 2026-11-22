"""Sanitise competing-client identity strings before agy forwards them upstream.

`agy` talks to daily-cloudcode-pa.googleapis.com **directly** — it never passes
through 9Router, so 9Router's ANTIGRAVITY_PROMPT_REWRITES sanitizer does not apply
on this path. The backend answers a filtered identity with a detail-free
`429 RESOURCE_EXHAUSTED`, which looks exactly like exhausted quota. This module
applies the same rewrites at the only point under our control: the argv we hand
to the binary.

Kept deliberately narrow about *what* it claims and broad about *wording*:

- The identity is what gets filtered, not one exact sentence, so the pattern
  anchors on "Hermes [Agent]" plus an optional Nous Research attribution.
- A miss is silent (a 429 that looks like quota), so patterns must be a little
  greedy — but bounded to a single sentence to avoid eating unrelated text.

This is a mitigation, not a proof. It cannot know the backend's real filter.
"""

from __future__ import annotations

import re

REPLACEMENT = "You are an AI assistant."


def _opencode_case(m: re.Match[str]) -> str:
    s = m.group(0)
    return "Antigravity" if s == "OpenCode" else "ANTIGRAVITY" if s == "OPENCODE" else "antigravity"


# Mirrors 9Router's ANTIGRAVITY_PROMPT_REWRITES (open-sse/config/appConstants.js)
# so both request paths behave alike. Applied in order, case-insensitively.
# ``repl`` is either a literal string or a callable taking the match.
REWRITES: tuple[tuple[re.Pattern[str], str | object], ...] = (
    # Competing-client identity, sentence anchored so unrelated prose survives.
    (re.compile(r"You are a Claude agent, built on Anthropic's Claude Agent SDK\.", re.I), ""),
    (
        re.compile(
            r"(?:^|(?<=[.!?]\s))(?:You are\s+)?Hermes(?:\s+Agent)?[^.\n]*?\bNous\s+Research\b[^.\n]*\.?"
            r"|(?:^|(?<=[.!?]\s))(?:You are\s+)?Hermes(?:\s+Agent)?\s*[,:;\u2014\u2013-]?[^.\n]*?\b(?:AI\s+assistant|AI\s+agent)\b[^.\n]*\.?"
            r"|(?:^|(?<=[.!?]\s))(?:You are\s+)?Hermes\s+Agent\b(?=\s*(?:[.!?\n]|$))",
            re.I | re.M,
        ),
        REPLACEMENT,
    ),
    # Claude Code prepends this; OpenAI-format clients leak it through.
    (re.compile(r"^x-anthropic-billing-header:[^\n]*(?:\r?\n)*", re.I | re.M), ""),
    (re.compile(r"opencode", re.I), _opencode_case),
)


def sanitize(text: str) -> str:
    """Rewrite filtered identities in ``text``. Returns the input unchanged on no match."""
    if not isinstance(text, str) or not text:
        return text
    out = text
    for pattern, repl in REWRITES:
        out = pattern.sub(repl, out)  # type: ignore[arg-type]
    return out


def sanitize_argv(args: list[str], print_flags: tuple[str, ...] = ("-p", "--print", "--prompt")) -> list[str]:
    """Return a copy of ``args`` with print-mode prompt values sanitised.

    Handles both ``-p TEXT`` and ``--print=TEXT``. Non-prompt arguments are
    untouched, so paths, models and flags pass through byte-identical.
    """
    out = list(args)
    i = 0
    while i < len(out):
        a = out[i]
        if a in print_flags:
            if i + 1 < len(out) and isinstance(out[i + 1], str):
                out[i + 1] = sanitize(out[i + 1])
            i += 2
            continue
        for f in print_flags:
            if f.startswith("--") and a.startswith(f + "="):
                head, _, val = a.partition("=")
                out[i] = f"{head}={sanitize(val)}"
                break
        i += 1
    return out
