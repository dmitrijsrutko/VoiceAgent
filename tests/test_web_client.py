"""Checks on the browser client.

The page's logic lives in native ES modules under `web/`. The parts with real
decisions in them — scheduling, gap counting, the half-duplex gate, autoplay —
are in `player.js`, which takes the DOM and the socket as callbacks, so
`tests/web/*.test.mjs` *executes* it under `node --test`. The test below runs
those.

What remains here are cheap structural checks for failures that actually
happened and that no executed test can see: an edit that deleted the microphone
block wholesale left the page throwing a ReferenceError on its first message
while every Python test still passed.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "src" / "voice_agent" / "web"
PAGE = WEB / "index.html"
NODE_TESTS = sorted((ROOT / "tests" / "web").glob("*.test.mjs"))

MIN_NODE = 22
"""Node detects ES module syntax in a plain `.js` file by default only from 22
(22.7). Older node reads `export` as a CommonJS syntax error, which would
report a broken module rather than an old runtime."""


def node_major() -> int | None:
    node = shutil.which("node")
    if node is None:
        return None
    version = subprocess.run([node, "--version"], capture_output=True, text=True, check=False)
    match = re.match(r"v(\d+)\.", version.stdout)
    return int(match.group(1)) if match else None


def why_node_is_unusable(major: int | None) -> str | None:
    if major is None:
        return "node is not installed — the page's executed tests did not run"
    if major < MIN_NODE:
        return f"node {major} is older than {MIN_NODE} — the page's executed tests did not run"
    return None


NODE_PROBLEM = why_node_is_unusable(node_major())
needs_node = pytest.mark.skipif(NODE_PROBLEM is not None, reason=NODE_PROBLEM or "")

BROWSER_GLOBALS = {
    "Array",
    "AudioContext",
    "AudioWorkletNode",
    "AudioWorkletProcessor",
    "Boolean",
    "Date",
    "Error",
    "Float32Array",
    "Map",
    "WeakMap",
    "Int16Array",
    "JSON",
    "Math",
    "Number",
    "Object",
    "Promise",
    "Set",
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
    "import",
    "new",
    "return",
    "super",
    "switch",
    "typeof",
    "while",
    "class",
    "constructor",
}


def source(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def page_script() -> str:
    """Every module the page loads, as one text. The page-level checks below
    care whether a name is defined *somewhere* the page can reach."""
    return "\n".join(source(path.name) for path in sorted(WEB.glob("*.js")))


def without_comments(text: str) -> str:
    """Drop `//` comments. A checker that reads its own explanations as code
    reports the prose rather than the program — this file did that twice."""
    return re.sub(r"(?<!:)//[^\n]*", "", text)


def code_only(script: str) -> str:
    """Drop the literal text inside template strings, keeping `${...}` parts.

    Prose in a template literal is data, not code: `already cached (${pct}%)`
    contains no call to a function named `cached`.
    """
    return re.sub(
        r"`[^`]*`",
        lambda m: " ".join(re.findall(r"\$\{([^{}]*)\}", m.group(0))),
        # Whole-line comments only: they are where the prose is, and a
        # parenthesis in prose reads as a call. A `//` mid-line may be code —
        # `${proto}//${location.host}` would lose its closing backtick.
        re.sub(r"^\s*//[^\n]*", "", script, flags=re.MULTILINE),
        flags=re.DOTALL,
    )


def defined_names(script: str) -> set[str]:
    script = code_only(script)
    patterns = (
        r"function\s+([A-Za-z_$][\w$]*)",
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)",
        r"class\s+([A-Za-z_$][\w$]*)",
        # object and class methods, which are not `function name(...)`
        r"\n\s{2,}([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{",
    )
    names = {name for pattern in patterns for name in re.findall(pattern, script)}
    for imported in re.findall(r"import\s*\{([^}]*)\}", script):
        # `paintListening as paint` defines `paint`, not `paintListening`.
        names.update(part.split(" as ")[-1].strip() for part in imported.split(","))
    # Parameters count as defined: a callback passed in and invoked by name is
    # not a missing function. Matching too much only ever adds to this set.
    for params in re.findall(r"\(([^()]*)\)\s*(?:=>|\{)", script):
        names.update(re.findall(r"[A-Za-z_$][\w$]*", params))
    return names


def called_names(script: str) -> set[str]:
    # An identifier followed by "(", not preceded by a dot (a method call) or
    # by another word character.
    return set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", code_only(script)))


@pytest.mark.parametrize(
    ("major", "problem"),
    [(None, "not installed"), (20, "older than 22"), (22, None), (25, None)],
)
def test_an_unusable_node_is_skipped_with_a_reason_that_says_so(
    major: int | None, problem: str | None
) -> None:
    reason = why_node_is_unusable(major)

    if problem is None:
        assert reason is None
    else:
        assert reason is not None and problem in reason


@needs_node
def test_the_player_logic_passes_its_executed_tests() -> None:
    """Scheduling, gaps, the half-duplex gate and the autoplay fallback, run
    against a fake AudioContext rather than matched as text."""
    assert NODE_TESTS, "no node tests found under tests/web"

    result = subprocess.run(
        ["node", "--test", *map(str, NODE_TESTS)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
@pytest.mark.parametrize("module", sorted(p.name for p in WEB.glob("*.js")))
def test_every_module_parses(module: str) -> None:
    result = subprocess.run(
        ["node", "--check", str(WEB / module)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_the_page_loads_its_entry_module() -> None:
    page = PAGE.read_text(encoding="utf-8")

    assert '<script type="module" src="/static/app.js"></script>' in page
    assert "<script>" not in page, "logic crept back inline, where nothing can execute it"


def test_every_module_an_import_names_exists() -> None:
    for path in WEB.glob("*.js"):
        for target in re.findall(r'from\s+"\./([^"]+)"', path.read_text(encoding="utf-8")):
            assert (WEB / target).exists(), f"{path.name} imports missing {target}"


def test_every_imported_name_is_exported_by_its_module() -> None:
    """Imports count as definitions below, so a deleted export would otherwise
    hide behind the import that still names it — and the module would fail to
    load in the browser, taking the whole page with it."""
    for path in WEB.glob("*.js"):
        text = path.read_text(encoding="utf-8")
        for names, target in re.findall(r'import\s*\{([^}]*)\}\s*from\s+"\./([^"]+)"', text):
            exporter = source(target)
            for name in (part.split(" as ")[0].strip() for part in names.split(",")):
                if not name:
                    continue
                exported = re.search(
                    rf"export\s+(?:async\s+)?(?:function|const|let|class)\s+{re.escape(name)}\b",
                    exporter,
                )
                assert exported, f"{path.name} imports {name!r}, which {target} does not export"


@pytest.mark.parametrize("module", sorted(p.name for p in WEB.glob("*.js")))
def test_every_function_a_module_calls_is_defined_or_imported(module: str) -> None:
    """The exact failure this file exists for. Deleting a definition while
    leaving its call sites is invisible to `node --check`. Checked per module:
    a name defined in *another* module is a ReferenceError unless imported."""
    text = source(module)
    missing = called_names(text) - defined_names(text) - BROWSER_GLOBALS - KEYWORDS

    assert not missing, f"{module} calls but never defines or imports: {sorted(missing)}"


@pytest.mark.parametrize(
    "element",
    ["input", "send", "listen", "mute", "log", "wrap", "form", "meta", "status"],
)
def test_every_element_the_script_reaches_for_exists_in_the_markup(element: str) -> None:
    page = PAGE.read_text(encoding="utf-8")

    assert f'getElementById("{element}")' in source("ui.js")
    assert f'id="{element}"' in page


def test_the_capture_pipeline_is_present() -> None:
    """Microphone capture is the one part with no server-side counterpart and
    no executed test, so nothing else would notice it going missing."""
    mic, worklet = source("mic.js"), source("capture-worklet.js")
    for fragment in (
        "getUserMedia",
        "audioWorklet.addModule",
        '"capture"',
        "createMediaStreamSource",
    ):
        assert fragment in mic, f"mic.js is missing {fragment}"
    assert "./capture-worklet.js" in mic, "the worklet file is not the one loaded"
    assert 'registerProcessor("capture"' in worklet


def test_the_microphone_is_sent_while_the_agent_speaks() -> None:
    """Chapter 8 removed the half-duplex gate: with `!speaking` back in the send
    path, the user can never be heard over the agent, and barge-in is dead."""
    app = source("app.js")
    send_path = re.search(r"function sendFrame\(.*?\n\}", app, re.DOTALL)

    assert send_path, "the microphone send path is gone"
    assert "speaking" not in without_comments(send_path.group(0))
    assert "buildMic(sampleRate, sendFrame)" in app


def test_the_microphone_asks_the_browser_to_cancel_the_agent_s_echo() -> None:
    """Full duplex rests on it: without echo cancellation the recognizer hears
    the agent's own voice, and the agent interrupts itself."""
    assert "echoCancellation: true" in source("mic.js")


def test_speaking_is_assigned_in_exactly_one_place() -> None:
    """A leaked playback hold muted the microphone for a whole session. The fix
    was to funnel every change through one transition-guarded setter."""
    app = without_comments(source("app.js"))
    assignments = re.findall(r"(?<![\w$.])speaking\s*=(?!=)", app)
    declarations = re.findall(r"\blet\s+speaking\s*=", app)

    assigned = len(assignments) - len(declarations)
    assert assigned == 1, f"speaking is assigned in {assigned} places outside its declaration"


def body_of(script: str, signature: str) -> str:
    body = script[script.index(signature) :]
    return body[: body.index("\n}")]


def test_nothing_scrolls_the_log_outside_the_helper(page_script: str) -> None:
    """Telemetry is the last thing appended to a turn, so an append that does
    not scroll is one nobody ever sees."""
    helper = body_of(source("ui.js"), "export function stick(")

    assert page_script.count("log.scrollTop =") == helper.count("log.scrollTop ="), (
        "something scrolls the log outside stick(); it will fight the helper"
    )


@pytest.mark.parametrize("appender", ["export function add(", "export function note("])
def test_both_appenders_scroll(appender: str) -> None:
    assert "stick(" in body_of(source("ui.js"), appender), f"{appender} appends without scrolling"


def test_the_reader_is_not_yanked_back_down() -> None:
    """Scrolled up reading earlier turns, a hard scroll-to-bottom on every
    fragment would be worse than the bug it fixes."""
    ui = source("ui.js")
    helper = body_of(ui, "export function stick(")
    callback = re.search(r"function stick\((\w+)\)", ui)
    assert callback, "stick() takes no callback, so it cannot enforce the ordering"

    assert "clientHeight" in helper, "stick() does not check whether we were at the bottom"
    assert helper.index("clientHeight") < helper.index(f"{callback.group(1)}()"), (
        "the check must happen before the mutation, since the mutation moves the bottom"
    )


def test_sending_a_message_drops_the_unheard_greeting_before_resuming() -> None:
    """The drop itself is executed in the node tests; its *order* in the wiring
    is not. Resuming first plays the greeting's first second, then cuts it."""
    submit = without_comments(body_of(source("app.js"), "form.onsubmit = (event) =>"))

    assert "player.dropUnheard()" in submit, "an unheard greeting is resumed on send"
    assert submit.index("player.dropUnheard()") < submit.index("player.resume()")
    # ...and both come after the message is sent: a browser that refuses the
    # audio context throws there, and that must cost the voice, not the words.
    assert submit.index("ws.send(userMessage(text))") < submit.index("player.dropUnheard()")
    assert submit.index("try {") < submit.index("player.resume()"), "an audio failure escapes"


def test_the_cached_token_percentage_cannot_divide_by_zero() -> None:
    """A provider that reports no usage yields a zero prompt-token count, and
    `NaN%` on screen is how you would find out."""
    assert "const pct = msg.warm_prompt_tokens" in source("app.js"), "the division is unguarded"
