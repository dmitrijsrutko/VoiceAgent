"""Static checks on the browser client.

There is no JavaScript in the quality gate, and that gap has already cost a
release: an edit deleted the microphone block wholesale, the page threw a
ReferenceError on its first message, every control stayed disabled — and every
Python test still passed. These are cheap checks for the failures that
actually happened, not a substitute for running the page.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "src" / "voice_agent" / "web" / "index.html"

BROWSER_GLOBALS = {
    "Array",
    "Audio",
    "AudioContext",
    "AudioWorkletNode",
    "AudioWorkletProcessor",
    "Blob",
    "Boolean",
    "Date",
    "Error",
    "Int16Array",
    "JSON",
    "Math",
    "Number",
    "Object",
    "Promise",
    "String",
    "URL",
    "WebSocket",
    "clearTimeout",
    "console",
    "document",
    "fetch",
    "isNaN",
    "navigator",
    "parseFloat",
    "parseInt",
    "registerProcessor",
    "requestAnimationFrame",
    "setInterval",
    "setTimeout",
    "window",
}

KEYWORDS = {
    "async",
    "await",
    "catch",
    "do",
    "else",
    "for",
    "function",
    "if",
    "new",
    "return",
    "super",
    "switch",
    "typeof",
    "while",
    "class",
    "constructor",
}


@pytest.fixture(scope="module")
def script() -> str:
    page = PAGE.read_text(encoding="utf-8")
    body = re.search(r"<script>(.*)</script>", page, re.DOTALL)
    assert body, "the page has no inline script"
    return body.group(1)


def code_only(script: str) -> str:
    """Drop the literal text inside template strings, keeping `${...}` parts.

    Prose in a template literal is data, not code: `already cached (${pct}%)`
    contains no call to a function named `cached`. Without this the checker
    reports on the wording of the UI, and a checker that fires on prose is one
    that gets ignored. The worklet source lives in a template literal too, so
    it is exempt from this analysis and covered by the capture-pipeline test.
    """
    return re.sub(
        r"`[^`]*`",
        lambda m: " ".join(re.findall(r"\$\{([^{}]*)\}", m.group(0))),
        script,
        flags=re.DOTALL,
    )


def defined_names(script: str) -> set[str]:
    patterns = (
        r"function\s+([A-Za-z_$][\w$]*)",
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)",
        r"class\s+([A-Za-z_$][\w$]*)",
        # class methods, which are not `function name(...)`
        r"\n\s{2,}([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{",
    )
    script = code_only(script)
    names = {name for pattern in patterns for name in re.findall(pattern, script)}
    # Parameters count as defined: a callback passed in and invoked by name is
    # not a missing function, and flagging it would train us to ignore this.
    # Matching too much is harmless — it only ever adds to the "defined" set.
    for params in re.findall(r"\(([^()]*)\)\s*(?:=>|\{)", script):
        names.update(re.findall(r"[A-Za-z_$][\w$]*", params))
    return names


def called_names(script: str) -> set[str]:
    # An identifier followed by "(", not preceded by a dot (a method call) or
    # by another word character.
    return set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", code_only(script)))


def test_every_function_the_page_calls_is_defined(script: str) -> None:
    """The exact failure this file exists for. Deleting a definition while
    leaving its call sites is invisible to every other test in the suite."""
    missing = called_names(script) - defined_names(script) - BROWSER_GLOBALS - KEYWORDS

    assert not missing, f"called but never defined: {sorted(missing)}"


@pytest.mark.parametrize(
    "element",
    ["input", "send", "listen", "mute", "log", "wrap", "form", "meta", "status"],
)
def test_every_element_the_script_reaches_for_exists_in_the_markup(
    script: str, element: str
) -> None:
    page = PAGE.read_text(encoding="utf-8")

    assert f'getElementById("{element}")' in script
    assert f'id="{element}"' in page


def test_the_capture_pipeline_is_present(script: str) -> None:
    """Microphone capture is the one part with no server-side counterpart, so
    nothing else in the suite would notice it going missing."""
    for fragment in (
        "AudioWorkletProcessor",  # the capture processor
        "registerProcessor",  # ...registered under a name
        '"capture"',  # ...the name buildMic asks for
        "audioWorklet.addModule",  # ...loaded from the Blob module
        "getUserMedia",  # microphone permission
        "createMediaStreamSource",  # wired into the graph
    ):
        assert fragment in script, f"the capture pipeline is missing {fragment}"


def test_the_half_duplex_gate_is_still_in_the_send_path(script: str) -> None:
    """Without `!speaking` the agent transcribes its own voice."""
    send_path = re.search(r"port\.onmessage.*?\n  \};", script, re.DOTALL)

    assert send_path, "the microphone send path is gone"
    assert "!speaking" in send_path.group(0)


def test_speaking_is_assigned_in_exactly_one_place(script: str) -> None:
    """A leaked playback hold muted the microphone for a whole session. The fix
    was to funnel every change through one transition-guarded setter."""
    assignments = re.findall(r"(?<![\w$])speaking\s*=(?!=)", script)
    declarations = re.findall(r"\blet\s+speaking\s*=", script)

    assigned = len(assignments) - len(declarations)
    assert assigned == 1, f"speaking is assigned in {assigned} places outside its declaration"


def body_of(script: str, signature: str) -> str:
    body = script[script.index(signature) :]
    return body[: body.index("\n}")]


def test_nothing_scrolls_the_log_outside_the_helper(script: str) -> None:
    """Telemetry is the last thing appended to a turn, so an append that does
    not scroll is one nobody ever sees — which is exactly how the `🔊 …` line
    ended up permanently below the fold."""
    helper = body_of(script, "function stick(")

    assert script.count("log.scrollTop =") == helper.count("log.scrollTop ="), (
        "something scrolls the log outside stick(); it will fight the helper"
    )


@pytest.mark.parametrize("appender", ["function add(", "function note("])
def test_both_appenders_scroll(script: str, appender: str) -> None:
    assert "stick(" in body_of(script, appender), f"{appender.strip()} appends without scrolling"


def test_the_reader_is_not_yanked_back_down(script: str) -> None:
    """Scrolled up reading earlier turns, a hard scroll-to-bottom on every
    fragment would be worse than the bug it fixes."""
    helper = body_of(script, "function stick(")
    callback = re.search(r"function stick\((\w+)\)", script)
    assert callback, "stick() takes no callback, so it cannot enforce the ordering"

    assert "clientHeight" in helper, "stick() does not check whether we were at the bottom"
    assert helper.index("clientHeight") < helper.index(f"{callback.group(1)}()"), (
        "the check must happen before the mutation, since the mutation moves the bottom"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_client_javascript_parses(script: str, tmp_path: Path) -> None:
    """The static checks above read the script as text and cannot see a syntax
    error. `node --check` can, and costs nothing where node exists."""
    source = tmp_path / "client.js"
    source.write_text(script, encoding="utf-8")

    result = subprocess.run(
        ["node", "--check", str(source)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def without_comments(source: str) -> str:
    """Drop `//` comments. A checker that reads its own explanations as code
    reports the prose rather than the program — this file has now done that
    twice, once on a template literal and once on a comment."""
    return re.sub(r"//[^\n]*", "", source)


def test_the_autoplay_fallback_does_not_revoke_the_clip_it_will_replay(script: str) -> None:
    """The greeting arrives before any user gesture, so browsers refuse to play
    it. The fallback holds the clip until the first click — which it cannot do
    if it has already revoked the object URL, as the first version did."""
    body = body_of(script, "function play(")
    fallback = without_comments(body[body.index(".play().catch(") :])

    assert "done()" not in fallback, "the fallback revokes the URL it intends to replay"
    assert "pointerdown" in fallback, "nothing waits for the gesture that unblocks audio"


def test_the_cached_token_percentage_cannot_divide_by_zero(script: str) -> None:
    """A provider that reports no usage yields a zero prompt-token count, and
    `NaN%` on screen is how you would find out."""
    assert "const pct = msg.warm_prompt_tokens" in script, "the division is unguarded"
