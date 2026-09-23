# ElevenLabs — Coding Agent Reference

ElevenLabs publishes no single coding-agent prompt like [AssemblyAI.md](AssemblyAI.md).
Its equivalent is split in two, both vendored or linked here.

**Live docs (always first).** The API changes; do not rely on memorized
parameter names. Fetch the index before writing ElevenLabs code:

- `https://elevenlabs.io/docs/llms.txt` — structured index
- `https://elevenlabs.io/docs/llms-full.txt` — everything
- append `.md` to any docs URL for that page as Markdown

**Skills (vendored snapshot).** [`elevenlabs/`](elevenlabs/) holds four skills
from [github.com/elevenlabs/skills](https://github.com/elevenlabs/skills),
copied verbatim (MIT) at commit `9edcbd4b80ed57b8e07a3f86ea520333969fbc3c`
(2026-09-09). The skills for music, sound effects, dubbing, voice changer and
voice isolator were left out.

| Read | When |
|------|------|
| [speech-to-text/references/realtime-server-side.md](elevenlabs/speech-to-text/references/realtime-server-side.md), [realtime-events.md](elevenlabs/speech-to-text/references/realtime-events.md), [realtime-commit-strategies.md](elevenlabs/speech-to-text/references/realtime-commit-strategies.md) | `stt/elevenlabs_stt.py` — Scribe realtime WS, partial vs committed transcripts, VAD vs manual commit |
| [text-to-speech/references/streaming.md](elevenlabs/text-to-speech/references/streaming.md), [voice-settings.md](elevenlabs/text-to-speech/references/voice-settings.md) | `tts/elevenlabs_tts.py` — streaming WS, model choice for latency, voice settings |
| [speech-engine/SKILL.md](elevenlabs/speech-engine/SKILL.md), [agents/SKILL.md](elevenlabs/agents/SKILL.md) | Background only — see below |

**Why we don't build on Speech Engine or ElevenAgents.** Both are managed
runtimes: ElevenLabs owns STT, TTS *and turn-taking*, and your server only
answers recognized user speech (Agents exposes one knob, `turn_eagerness`).
Neither documents a way for the agent to speak unprompted. This project's point
is its own turn-taking and initiative (`turn.py`, `initiative.py`) — an agent
that interjects — so we use ElevenLabs STT and TTS directly behind our own
protocols and keep the orchestration.

To refresh the snapshot: shallow-clone the repo, re-copy the four folders, and
update the commit above.
