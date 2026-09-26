"""`uv run voice-agent` — serve the chat page and the conversation socket."""

import argparse
import dataclasses
import logging
import os
import time
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from voice_agent import roles
from voice_agent.config import (
    DEFAULT_INITIATIVE_DELAYS,
    Settings,
    load_settings,
    parse_delays,
    parse_directory,
)
from voice_agent.errors import ConfigError
from voice_agent.llm.registry import BY_NAME, default_choice, offered
from voice_agent.stt import registry as stt_registry
from voice_agent.tts import registry as tts_registry


def main() -> None:
    # Loaded before settings are read, and only here: library code never
    # reaches for a .env file, so importing this package has no side effects.
    load_dotenv()
    # A misconfiguration is a message, not a traceback: a typo in a flag or a
    # variable is the one error a user is expected to hit.
    try:
        settings = load_settings()
    except ConfigError as exc:
        raise SystemExit(f"voice-agent: {exc}") from exc

    parser = argparse.ArgumentParser(description="Run the voice-agent server.")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument(
        "--tts",
        default=settings.voice_provider,
        choices=[*tts_registry.BUILDERS, tts_registry.NO_VOICE],
        help="speech synthesis backend, or 'none' to stay silent (default: %(default)s)",
    )
    parser.add_argument("--voice", default=settings.voice, help="voice id or name for the backend")
    parser.add_argument(
        "--voice-gender",
        default=settings.voice_gender,
        choices=["female", "male", "neutral"],
        help="how the agent speaks about itself in languages that mark the "
        "speaker's gender (default: %(default)s — match this to --voice)",
    )
    parser.add_argument(
        "--stt",
        default=settings.ears_provider,
        choices=[*stt_registry.EARS, stt_registry.NO_EARS],
        help="speech recognition backend, or 'none' to stay deaf (default: %(default)s)",
    )
    parser.add_argument(
        "--vad-silence",
        type=float,
        default=settings.vad_silence,
        metavar="SECONDS",
        help="pause length that ends a spoken turn (default: the recognizer's own)",
    )
    parser.add_argument(
        "--initiative",
        default=None,
        metavar="SECONDS,...",
        help="silences at which the agent considers speaking unprompted, or 'off' "
        f"for the purely reactive agent (default: {DEFAULT_INITIATIVE_DELAYS})",
    )
    parser.add_argument(
        "--role",
        default=settings.role,
        choices=roles.available(),
        help="the role pre-selected on the start screen, from prompts/roles/; each "
        "conversation still picks its own (default: %(default)s)",
    )
    parser.add_argument(
        "--sessions",
        default=None,
        metavar="DIR",
        help="where to write the conversation record, or 'off' (default: sessions)",
    )
    parser.add_argument(
        "--trace",
        default=None,
        metavar="DIR",
        help="where to write the technical trace, or 'off' (default: traces)",
    )
    parser.add_argument(
        "--purge-sessions",
        action="store_true",
        help="delete every recorded conversation, then exit",
    )
    parser.add_argument(
        "--list-voices",
        action="store_true",
        help="list the voices this account can actually use, then exit",
    )
    parser.add_argument(
        "--bench-llm",
        nargs="*",
        metavar="PROVIDER[:MODEL]",
        help="time to first token per provider, over a few short billed calls, then exit "
        "(default: deepseek, openai, anthropic:claude-haiku-4-5, anthropic)",
    )
    parser.add_argument(
        "--replay-thinker",
        nargs="*",
        metavar="SCENARIO",
        help="replay scripted conversations from tests/scenarios/ through the inner voice "
        "and score it, then exit; billed, one call per pause (default: all)",
    )
    args = parser.parse_args()
    try:
        settings = with_flags(settings, args)
    except ConfigError as exc:
        raise SystemExit(f"voice-agent: {exc}") from exc

    start_logging(settings)

    if args.bench_llm is not None:
        from voice_agent.bench import main as bench

        bench(args.bench_llm)
        return

    if args.replay_thinker is not None:
        from voice_agent.replay import main as replay

        replay(args.replay_thinker)
        return

    if args.purge_sessions:
        purge_sessions(settings.sessions)
        return

    if args.list_voices:
        list_voices(args.tts)
        return

    from voice_agent.server import create_app

    sex = {"female": "♀", "male": "♂", "neutral": "·"}[args.voice_gender]
    spoken = tts_registry.default_choice(tts_registry.offered(args.tts))
    title = f" {tts_registry.BY_NAME[spoken].title}" if spoken else ""
    voice = f"{args.tts}{title} {sex}" if args.tts != "none" else "silent"
    ears = args.stt if args.stt != "none" else "deaf"
    try:
        app = create_app(settings=settings)
    except ConfigError as exc:
        raise SystemExit(f"voice-agent: {exc}") from exc
    delays = settings.initiative
    clock = "+".join(f"{d:g}s" for d in delays) if delays else "reactive"
    menu = offered()
    print(
        f"voice-agent → http://{args.host}:{args.port}  "
        f"({BY_NAME[default_choice(menu)].title} by default · {len(menu)} models "
        f"· 🔊 {voice} · 🎤 {ears} · ⏱ {clock} "
        f"· 📝 {settings.sessions or 'off'} · 🔬 {settings.trace or 'off'})"
    )
    # Said at every start: a language this recognizer lacks is not refused but
    # transcribed as confident nonsense, which the agent then answers.
    if args.stt == "assemblyai":
        from voice_agent.stt.assemblyai_stt import LANGUAGES

        print(
            f"   ears speak {len(LANGUAGES)} languages ({' '.join(LANGUAGES)}). "
            "Anything else is transcribed as nonsense rather than refused — "
            "use --stt elevenlabs for it."
        )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def with_flags(settings: Settings, args: argparse.Namespace) -> Settings:
    """The environment's settings, overridden by whatever was given on the
    command line. Every flag defaults to its variable, so this is one mechanism."""
    return dataclasses.replace(
        settings,
        host=args.host,
        port=args.port,
        voice_provider=args.tts,
        voice=args.voice,
        voice_gender=args.voice_gender,
        ears_provider=args.stt,
        vad_silence=args.vad_silence,
        role=args.role,
        initiative=settings.initiative
        if args.initiative is None
        else parse_delays(args.initiative),
        sessions=settings.sessions if args.sessions is None else parse_directory(args.sessions),
        trace=settings.trace if args.trace is None else parse_directory(args.trace),
    )


def start_logging(settings: Settings | None = None) -> None:
    """Configure logging: the console at INFO, the trace at DEBUG.

    The console's level is set on the **handler**, not inherited from the root
    logger. Propagation consults handler levels and ignores ancestor logger
    levels, so `voice_agent` at DEBUG for the trace would otherwise flood the
    terminal.
    """
    from voice_agent.trace import TraceHandler, install, open_trace

    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.WARNING)  # third-party loggers unchanged
    root.addHandler(console)

    settings = settings or load_settings()
    kept = open_log_file(settings.logs)
    if kept is not None:
        root.addHandler(kept)
        # Our own INFO reaches the file; the console's handler still holds at WARNING.
        logging.getLogger("voice_agent").setLevel(logging.INFO)
        logging.getLogger("voice_agent").info(
            "process started · image %s", os.environ.get("FLY_IMAGE_REF", "local")
        )

    tracing = open_trace(settings.trace)
    install(tracing)
    if tracing is not None:
        project = logging.getLogger("voice_agent")
        project.setLevel(logging.DEBUG)  # our own INFO reaches the trace
        project.addHandler(TraceHandler())  # and only the trace


LOG_FILE_BYTES = 5_000_000
LOG_FILES_KEPT = 10
"""At most ~55 MB of log on the volume, oldest dropped first."""


def open_log_file(directory: Path | None) -> logging.Handler | None:
    """A rotating log file in `directory`, for a history that outlives the
    process: the machine's own log buffer is ~100 lines and is gone after a
    deploy. INFO and above, UTC timestamps. `None` when switched off."""
    if directory is None:
        return None
    from logging.handlers import RotatingFileHandler

    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        directory / "voice-agent.log",
        maxBytes=LOG_FILE_BYTES,
        backupCount=LOG_FILES_KEPT,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%SZ"
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    return handler


def purge_sessions(directory: Path | None) -> None:
    """Delete every recorded conversation.

    The retention policy is "keep everything", so this is the whole of the
    delete half of it — which means it asks first. These are transcripts of
    things somebody said out loud, and there is no second copy.
    """
    if directory is None:
        print("Conversations are not being recorded.")
        return
    files = sorted(directory.glob("*.md"))
    if not files:
        print(f"No conversations in {directory}.")
        return
    print(f"{len(files)} conversation(s) in {directory}, from {files[0].name}.")
    if input("Delete them all? [y/N] ").strip().casefold() not in ("y", "yes"):
        print("Left alone.")
        return
    for path in files:
        path.unlink()
    print(f"Deleted {len(files)}.")


def list_voices(provider: str) -> None:
    """Which voices an account may use is not stable and not inferable from
    documentation — a wrong id fails at synthesis time with a payment error,
    not at startup. Ask the backend instead of trusting a written-down id."""
    import asyncio

    from voice_agent.errors import VoiceAgentError
    from voice_agent.tts.elevenlabs_tts import ElevenLabsTTS

    if provider != "elevenlabs":
        print(f"--tts {provider} has no voices to list.")
        return
    backend = ElevenLabsTTS()

    try:
        voices = asyncio.run(backend.list_voices())
    except VoiceAgentError as exc:
        raise SystemExit(f"could not list voices: {exc}") from exc

    usable = [v for v in voices if v.usable]
    print(f"{len(usable)} of {len(voices)} voices are usable on this account:\n")
    for v in usable:
        print(f"  {v.id}  {v.name}")
    blocked = [v for v in voices if not v.usable]
    if blocked:
        print(f"\n{len(blocked)} visible but not usable on this plan:\n")
        for v in blocked:
            print(f"  {v.id}  {v.name}")


if __name__ == "__main__":
    main()
