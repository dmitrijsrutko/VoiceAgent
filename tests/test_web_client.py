"""Static checks on the browser client.

There is no JavaScript in the quality gate, and that gap has already cost a
release: an edit deleted the microphone block wholesale, the page threw a
ReferenceError on its first message, every control stayed disabled — and every
Python test still passed. These are cheap checks for the failures that
actually happened, not a substitute for running the page.
"""

import re
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


def defined_names(script: str) -> set[str]:
    patterns = (
        r"function\s+([A-Za-z_$][\w$]*)",
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)",
        r"class\s+([A-Za-z_$][\w$]*)",
        # class methods, which are not `function name(...)`
        r"\n\s{2,}([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{",
    )
    return {name for pattern in patterns for name in re.findall(pattern, script)}


def called_names(script: str) -> set[str]:
    # An identifier followed by "(", not preceded by a dot (a method call) or
    # by another word character.
    return set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", script))


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
