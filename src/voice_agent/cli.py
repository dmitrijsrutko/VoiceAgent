"""`uv run voice-agent` — serve the chat page and the conversation socket."""

import argparse
import logging
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from voice_agent.config import DEFAULT_INITIATIVE_DELAYS, load_settings


def main() -> None:
    # Loaded before settings are read, and only here: library code never
    # reaches for a .env file, so importing this package has no side effects.
    load_dotenv()
    settings = load_settings()

    parser = argparse.ArgumentParser(description="Run the voice-agent server.")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument(
        "--provider",
        default=settings.provider,
        choices=["deepseek", "openai", "anthropic"],
        help="reasoning engine backend (default: %(default)s)",
    )
    parser.add_argument("--model", default=settings.model, help="override the provider's default")
    parser.add_argument(
        "--tts",
        default=settings.voice_provider,
        choices=["elevenlabs", "openai", "none"],
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
        choices=["assemblyai", "elevenlabs", "none"],
        help="speech recognition backend, or 'none' to stay deaf (default: %(default)s)",
    )
    parser.add_argument(
        "--vad-silence",
        type=float,
        default=settings.vad_silence,
        metavar="SECONDS",
        help="pause length that ends a spoken turn (default: 1.5)",
    )
    parser.add_argument(
        "--initiative",
        default=None,
        metavar="SECONDS,...",
        help="silences at which the agent considers speaking unprompted, or 'off' "
        # Read, not restated: this said 7,20,45 for two chapters after those
        # were no longer the delays.
        f"for the purely reactive agent (default: {DEFAULT_INITIATIVE_DELAYS})",
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
    args = parser.parse_args()

    # create_app() reads these back out of the environment, so the flags and
    # the env vars stay one mechanism rather than two.
    import os

    os.environ["VOICE_AGENT_PROVIDER"] = args.provider
    os.environ["VOICE_AGENT_TTS"] = args.tts
    os.environ["VOICE_AGENT_STT"] = args.stt
    if args.model:
        os.environ["VOICE_AGENT_MODEL"] = args.model
    if args.voice:
        os.environ["VOICE_AGENT_VOICE"] = args.voice
    os.environ["VOICE_AGENT_VOICE_GENDER"] = args.voice_gender
    if args.vad_silence:
        os.environ["VOICE_AGENT_VAD_SILENCE"] = str(args.vad_silence)
    if args.initiative is not None:
        os.environ["VOICE_AGENT_INITIATIVE"] = args.initiative
    if args.sessions is not None:
        os.environ["VOICE_AGENT_SESSIONS"] = args.sessions
    if args.trace is not None:
        os.environ["VOICE_AGENT_TRACE"] = args.trace

    start_logging()

    if args.bench_llm is not None:
        from voice_agent.bench import main as bench

        bench(args.bench_llm)
        return

    if args.purge_sessions:
        purge_sessions(load_settings().sessions)
        return

    if args.list_voices:
        list_voices(args.tts)
        return

    from voice_agent.errors import ConfigError
    from voice_agent.server import create_app

    sex = {"female": "♀", "male": "♂", "neutral": "·"}[args.voice_gender]
    voice = f"{args.tts} {sex}" if args.tts != "none" else "silent"
    ears = args.stt if args.stt != "none" else "deaf"
    # A misconfiguration is a message, not a traceback. It is the one error a
    # user is *expected* to hit — a typo in a flag — and burying the sentence
    # that says which flag under twenty lines of stack helps nobody.
    try:
        # Re-read, so a bad --initiative is rejected here rather than inside the
        # first connection.
        settings = load_settings()
        app = create_app()
    except ConfigError as exc:
        raise SystemExit(f"voice-agent: {exc}") from exc
    delays = settings.initiative
    clock = "+".join(f"{d:g}s" for d in delays) if delays else "reactive"
    print(
        f"voice-agent → http://{args.host}:{args.port}  "
        f"({args.provider} · 🔊 {voice} · 🎤 {ears} · ⏱ {clock} "
        f"· 📝 {settings.sessions or 'off'} · 🔬 {settings.trace or 'off'})"
    )
    # Said out loud at every start, because the failure this prevents is silent.
    # A language this backend does not know is not refused — it is transcribed
    # into confident nonsense, which the agent then answers. Measured: spoken
    # Russian came back as "Раскажем не pravalo вывnutriny produkt kitaia", and
    # nothing anywhere said the recognizer was out of its depth.
    if args.stt == "assemblyai":
        from voice_agent.stt.assemblyai_stt import LANGUAGES

        print(
            f"   ears speak {len(LANGUAGES)} languages ({' '.join(LANGUAGES)}). "
            "Anything else is transcribed as nonsense rather than refused — "
            "use --stt elevenlabs for it."
        )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def start_logging() -> None:
    """Configure logging, which until Chapter 11 nothing in this project did.

    The only line that touched it was uvicorn's own `log_level`, which sets
    uvicorn's loggers and not ours, so all eight `logger.info` calls under
    `src/voice_agent/` went nowhere — which is how the initiative clock's
    provider failures stayed invisible.

    The console's level is set on the **handler**, not inherited from the root
    logger. Propagation consults handler levels and ignores ancestor logger
    levels, so putting `voice_agent` at DEBUG to feed the trace also pushed
    every INFO line to the terminal — a recogniser reconnect, a failed warm, a
    turn ending — until this was written the way it is now.
    """
    from voice_agent.trace import TraceHandler, install, open_trace

    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.WARNING)  # third-party loggers unchanged
    root.addHandler(console)

    tracing = open_trace(load_settings().trace)
    install(tracing)
    if tracing is not None:
        project = logging.getLogger("voice_agent")
        project.setLevel(logging.DEBUG)  # our own INFO reaches the trace
        project.addHandler(TraceHandler())  # and only the trace


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
    from voice_agent.tts import create_tts

    backend = create_tts(provider)
    if backend is None:
        print("--tts none has no voices.")
        return

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
