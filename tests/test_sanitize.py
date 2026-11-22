"""Tests for the agy-path prompt sanitizer.

The gap this closes: agy talks to Google directly, so 9Router's rewrite list
never runs on this path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from antigravity_swap.sanitize import sanitize, sanitize_argv  # noqa: E402

IDENTITY = "You are Hermes Agent, an intelligent AI assistant created by Nous Research."


def test_rewrites_canonical_identity():
    out = sanitize(IDENTITY + " You are helpful.")
    assert "Hermes" not in out
    assert "Nous Research" not in out
    assert out.startswith("You are an AI assistant.")
    assert "You are helpful." in out, "instruksi sesudah identitas harus utuh"


@pytest.mark.parametrize(
    "text",
    [
        "You are Hermes Agent.",
        "You are Hermes Agent, built by Nous Research.",
        "Hermes Agent, an intelligent AI assistant created by Nous Research",
        "You are Hermes Agent \u2014 an intelligent AI assistant created by Nous Research.",
        "You are Hermes Agent, a helpful AI assistant built by Nous Research.",
        "You are Hermes Agent, a capable AI agent created by Nous Research.",
        "You are Hermes Agent: an autonomous AI assistant made by Nous Research.",
        "Context. You are Hermes Agent, a brilliant AI assistant developed by Nous Research. Then obey.",
    ],
)
def test_catches_variants(text):
    out = sanitize(text)
    assert out != text, f"harus di-rewrite: {text}"
    assert "Hermes" not in out, f"identitas tersisa: {out}"
    assert "Nous Research" not in out, f"vendor tersisa: {out}"


@pytest.mark.parametrize(
    "text",
    [
        "You are a helpful coding assistant.",
        "The Hermes rocket launched successfully.",
        "Nous Research publishes open models.",
        "You are Antigravity, a powerful agentic AI coding assistant designed by the Google Deepmind team.",
        "You are Claude Code, Anthropic's official CLI for Claude.",
    ],
)
def test_leaves_unrelated_text_untouched(text):
    assert sanitize(text) == text


def test_other_rewrites_still_apply():
    assert sanitize("x-anthropic-billing-header: abc\nrest") == "rest"
    assert sanitize("built with OpenCode") == "built with Antigravity"
    assert sanitize("You are a Claude agent, built on Anthropic's Claude Agent SDK.") == ""


def test_argv_short_form():
    args = ["--model", "gemini-3.8-flash-high", "-p", IDENTITY, "--dangerously-skip-permissions"]
    out = sanitize_argv(args)
    assert "Hermes" not in out[3]
    assert out[0:3] == args[0:3], "argumen non-prompt tidak boleh berubah"
    assert out[4] == args[4]


def test_argv_long_form_equals():
    args = [f"--print={IDENTITY}"]
    out = sanitize_argv(args)
    assert out[0].startswith("--print=")
    assert "Hermes" not in out[0]


def test_argv_long_form_separate():
    args = ["--print", IDENTITY]
    out = sanitize_argv(args)
    assert "Hermes" not in out[1]


def test_argv_does_not_mutate_input():
    args = ["-p", IDENTITY]
    before = list(args)
    sanitize_argv(args)
    assert args == before


def test_argv_ignores_non_prompt_values():
    args = ["--model", "Hermes Agent, an intelligent AI assistant created by Nous Research", "-p", "ok"]
    out = sanitize_argv(args)
    assert out[1] == args[1], "nilai --model tidak boleh disentuh"
    assert out[3] == "ok"


def test_empty_and_none_safe():
    assert sanitize("") == ""
    assert sanitize_argv([]) == []
