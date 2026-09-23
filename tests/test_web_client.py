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
    "URLSearchParams",
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
    [
        "input",
        "send",
        "listen",
        "mute",
        "log",
        "wrap",
        "form",
        "meta",
        "status",
        "start",
        "begin",
        "start-notices",
        "start-stack",
    ],
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


def test_the_socket_is_not_opened_by_loading_the_page() -> None:
    """The whole point of the start screen. Opening the socket *is* starting the
    conversation — the server greets, and starts the clock that decides whether
    to speak into a silence — so a page that opens one on load greets somebody
    who has not said they are ready, and cannot play the greeting anyway,
    autoplay being forbidden without a gesture."""
    app = without_comments(source("app.js"))
    connect = re.search(r"function connect\(\).*?\n\}", app, re.DOTALL)

    assert connect, "connect() is gone"
    assert app.count("new WebSocket(") == 1, "more than one place opens a socket"
    assert "new WebSocket(" in connect.group(0), "the socket is opened outside connect()"


def test_starting_gives_the_browser_its_gesture_before_anything_else() -> None:
    """`player.resume()` must be the first statement of the click handler and
    must not sit behind an `await`: user activation is what permits audio, and
    it does not survive being handed to a later task. Losing it puts the page
    back on 'click anywhere to hear the agent', which is the failure this
    change exists to remove."""
    app = without_comments(source("app.js"))
    handler = re.search(r"begin\.onclick\s*=\s*async\s*\(\)\s*=>\s*\{(.*?)\n\};", app, re.DOTALL)

    assert handler, "nothing handles the start button"
    body = [line.strip() for line in handler.group(1).strip().splitlines() if line.strip()]
    assert body[0] == "const resumed = player.resume();", f"the gesture is not first: {body[0]!r}"
    # Awaiting *before* the resume would hand the gesture to a later task and
    # lose it; awaiting the resume itself is what keeps the greeting from
    # racing it.
    gesture = body.index("const resumed = player.resume();")
    assert not any("await" in line for line in body[:gesture]), "an await above the gesture"
    assert "await resumed" in handler.group(1), "the resume is not waited for"
    assert body.index("connect();") > body.index("await resumed.catch(() => {});"), (
        "the socket opens before the audio context is running, and the greeting will race it"
    )


def test_the_microphone_is_asked_for_before_the_greeting_can_play() -> None:
    """Asked on `ready`, the permission prompt appeared while the greeting was
    already playing, and on a phone nothing is heard behind that prompt — the
    intro was lost. The socket, which is what makes the server greet, opens
    only once the prompt has been answered."""
    app = without_comments(source("app.js"))
    handler = re.search(r"begin\.onclick\s*=\s*async\s*\(\)\s*=>\s*\{(.*?)\n\};", app, re.DOTALL)

    assert handler, "nothing handles the start button"
    body = handler.group(1)
    assert "await buildMic(" in body, "the start button no longer asks for the microphone"
    assert body.index("await buildMic(") < body.rindex("connect();"), (
        "the socket opens before the microphone is asked for"
    )


def test_a_refused_microphone_is_not_asked_for_again_over_the_greeting() -> None:
    """Refused on the start click, the old `ready` path asked again — a second
    prompt over the greeting on a phone, and a second error in the log."""
    app = without_comments(source("app.js"))

    assert "micRefused = true;" in app
    assert "if (msg.ears && !micRefused) beginListening();" in app


def test_the_utterance_being_spoken_stays_last_in_the_log() -> None:
    """A reply to the previous utterance can start after the user has already
    carried on; appended below the live bubble, it put the user's next words
    above the reply they followed. Seen live on the deployed instance."""
    ui = without_comments(source("ui.js"))
    app = without_comments(source("app.js"))

    assert "wrap.insertBefore(el, below)" in ui
    assert 'live = add("", "msg user volatile"); pinLast(live);' in app
    assert "pinLast(null)" in app, "the live bubble is never released"


def test_listening_starts_itself_once_the_agent_is_ready() -> None:
    """Somebody who has just pressed 'start conversation' has said they are
    ready. Making them then press 'listen' is asking twice."""
    app = without_comments(source("app.js"))

    assert "if (msg.ears && !micRefused) beginListening();" in app
    # In the `ready` handler and nowhere earlier: that frame is what confirms
    # the rate the microphone was built at, and that the socket can carry it.
    auto_listen = "if (msg.ears && !micRefused) beginListening();"
    assert app.index("ready(msg) {") < app.index(auto_listen)


def test_the_page_carries_a_default_facts_block() -> None:
    """The start screen says what beginning entails, from facts the server
    writes into the page. Served any other way, an empty object has to leave a
    start screen that claims nothing rather than one that throws."""
    from voice_agent.server import FACTS_BLOCK

    assert FACTS_BLOCK in PAGE.read_text(encoding="utf-8")
    assert 'getElementById("facts")' in source("start.js")


def test_the_chosen_stack_travels_with_the_socket() -> None:
    """The socket opening is what starts a conversation, so the choice has to
    be on it. A selector the server never hears is a selector that lies."""
    app = without_comments(source("app.js"))
    start = without_comments(source("start.js"))
    connect = re.search(r"function connect\(\).*?\n\}", app, re.DOTALL)

    assert connect, "connect() is gone"
    assert "stackQuery()" in connect.group(0)
    assert 'for (const group of ["llm", "stt"])' in start, "both halves of the stack must be sent"


def test_the_languages_notice_follows_the_chosen_ears() -> None:
    """Scribe hears 100 languages and AssemblyAI 18, and which is running is
    the visitor's choice now — so a notice fixed at page load would describe a
    recognizer they did not pick."""
    start = without_comments(source("start.js"))

    assert 'startStack.addEventListener("change"' in start
    assert "languages: <code>" in start


def test_the_start_screen_warns_about_no_particular_language() -> None:
    """The languages line is the fact; which of them a visitor might have
    wanted is theirs to read off it, not a warning the page singles out."""
    assert "russian" not in source("app.js").lower()


def test_the_voice_is_a_group_of_one_that_is_never_sent() -> None:
    """`openai_tts.py` exists, but it waits for the whole reply before
    synthesising — offering it as a peer would be offering a worse agent. The
    voice is drawn like the other two parts of the stack, with one option, and
    the socket never carries a choice of it."""
    start = without_comments(source("start.js"))
    choosing = re.findall(r'choose\("(\w+)"', start)

    assert choosing == ["llm", "stt", "tts"], f"the pickers changed: {choosing}"
    assert "{ single: true }" in start, "the voice is no longer drawn as a group of one"
    assert 'for (const group of ["llm", "stt"])' in start, "the socket now carries something else"


def test_each_picker_draws_its_default_first() -> None:
    """The server lists providers in registry order — DeepSeek before
    Anthropic — which put the default on the right."""
    start = without_comments(source("start.js"))

    assert "sort((a, b) => Number(!!b.default) - Number(!!a.default))" in start
    assert "ordered.map(" in start
