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
| [realtime-tts](https://elevenlabs.io/docs/eleven-api/guides/how-to/websockets/realtime-tts), [realtime-tdd](https://elevenlabs.io/docs/eleven-api/guides/how-to/websockets/realtime-tdd), [tts-vs-ttd](https://elevenlabs.io/docs/eleven-api/guides/how-to/websockets/tts-vs-ttd-websockets) | `tts/elevenlabs_dialogue_tts.py` — which socket a model speaks over; both protocols side by side |
| [speech-engine/SKILL.md](elevenlabs/speech-engine/SKILL.md), [agents/SKILL.md](elevenlabs/agents/SKILL.md) | Background only — see below |

**Two synthesis sockets, and the model decides which (checked 2026-09-30).** The
TTS socket (`/v1/text-to-speech/{voice_id}/stream-input`) carries **no
`eleven_v3` or `eleven_v4` model**. Eleven v4 and v4 Turbo are served by the
**Text to Dialogue** socket (`/v1/text-to-dialogue/stream-input`), whose protocol
differs in every place that matters:

- The **first message must register `voices`** — one voice for `eleven_v4_turbo`,
  up to ten for `eleven_v4` — and text then travels as
  `{"inputs": [{"text", "voice_id"}], "flush"?, "close_socket"?, "keep_alive"?}`.
- A reply ends with `close_socket` (flush the tail, send `is_final`, close), not
  with the TTS socket's empty text. `flush` forces generation without closing;
  `keep_alive` resets a fixed **20 s** inactivity timer.
- Responses are **snake_case**: `is_final` (not `isFinal`),
  `is_final_audio_for_turn`, and `alignment`/`normalized_alignment` with
  `char_start_times_ms` / `char_durations_ms` — and only when
  `sync_alignment=true` is on the query string.
- The server buffers to its **own fixed threshold** (~40 characters and 8 words)
  before the first partial audio; there is no `chunk_length_schedule` to set.
- Concurrency: a dialogue socket holds one session from a **separate pool** for
  its whole lifetime, where a TTS socket counts against the plan's limit only
  while it is generating. This project opens one per reply, so four live
  conversations hold at most four.
- **A socket with no client message for 20 s is dropped** — measured on *both*
  sockets: an error frame (`input_timeout_exceeded` on the dialogue socket) then
  close code 1008. `keep_alive` resets the dialogue socket's timer; nothing here
  sends it, because the socket opens before the reasoning engine's first token
  and the worst first token in 125 recorded replies is 4.5 s.

Measured here (2026-09-30, `pcm_24000`, one sentence, both sockets): v4 Turbo's
alignment arrives on **every** chunk and is timed from that chunk's own first
sample — which is what `heard.py` already assumes — while `stream-input` times a
whole segment on its first message and sends the rest untimed.

**Why we don't build on Speech Engine or ElevenAgents.** Both are managed
runtimes: ElevenLabs owns STT, TTS *and turn-taking*, and your server only
answers recognized user speech (Agents exposes one knob, `turn_eagerness`).
Neither documents a way for the agent to speak unprompted. This project's point
is its own turn-taking and initiative (`turn.py`, `initiative.py`) — an agent
that interjects — so we use ElevenLabs STT and TTS directly behind our own
protocols and keep the orchestration.

To refresh the snapshot: shallow-clone the repo, re-copy the four folders, and
update the commit above.
