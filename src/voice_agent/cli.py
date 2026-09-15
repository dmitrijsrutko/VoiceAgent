"""`uv run voice-agent` — serve the chat page and the conversation socket."""

import argparse

import uvicorn
from dotenv import load_dotenv

from voice_agent.config import load_settings


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
        "--stt",
        default=settings.ears_provider,
        choices=["elevenlabs", "none"],
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
    if args.vad_silence:
        os.environ["VOICE_AGENT_VAD_SILENCE"] = str(args.vad_silence)

    if args.bench_llm is not None:
        from voice_agent.bench import main as bench

        bench(args.bench_llm)
        return

    if args.list_voices:
        list_voices(args.tts)
        return

    from voice_agent.server import create_app

    voice = args.tts if args.tts != "none" else "silent"
    ears = args.stt if args.stt != "none" else "deaf"
    print(
        f"voice-agent → http://{args.host}:{args.port}  ({args.provider} · 🔊 {voice} · 🎤 {ears})"
    )
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


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
