# Deploying — the server off localhost

The operational half of Chapter 14. `CHANGELOG.md` has the reasoning; this is
the runbook. `fly.toml` is the configuration itself, and its comments say why
each setting is what it is.

## The shape, and why it is not negotiable

One always-on machine, one region, one volume. That is not a starting point to
scale from later — it is what the code currently *is*:

- `sessions.py` keeps conversations in this process's memory, so a conversation's
  link only means anything to the machine that minted it. A second instance
  would 404 half the links.
- `greeting.py` caches the opening line's PCM per process, and `server.py`'s
  lifespan pays a 3.1 s synthesis cold start to fill it. A machine that stops
  charges that to whoever arrives next.
- The transport is a long-lived WebSocket carrying PCM in both directions, so
  nothing serverless can host it.

Persistence across restarts, and with it more than one machine, is a later
chapter. Until then, restarting the server loses every conversation, exactly as
it always has.

## First time

```bash
brew install flyctl          # or: curl -L https://fly.io/install.sh | sh
fly auth login

fly launch --no-deploy --copy-config --name voice-agent-chapters --region iad
fly volumes create data --region iad --size 1
```

`--copy-config` keeps the `fly.toml` in this repo rather than generating one.
Change `app` in `fly.toml` if that name is taken — Fly app names are unique across all of Fly.io, not just your organisation, which is how
`voice-agent-demo` turned out to be gone.

### Secrets

Never in `fly.toml`, never in the image — `fly secrets set` holds them and
restarts the machine with them in the environment.

```bash
fly secrets set \
  ASSEMBLYAI_API_KEY=...   `# ears (chapter 12)` \
  ELEVENLABS_API_KEY=...   `# voice (chapter 2)` \
  DEEPSEEK_API_KEY=...     `# reasoning (chapter 1)`
```

**Which keys are set decides what visitors can pick.** The start screen offers
the models named in `llm/registry.py`, and only those whose provider holds a key
here — so adding `DEEPSEEK_API_KEY` puts the three DeepSeek tiers on the start
screen, and removing a key takes its models off it. Since chapter 19 the model
is chosen per conversation on that screen, so no environment variable names a
default: it is Haiku 4.5. `VOICE_AGENT_STT` still names the default ears.

A backend with no key is never offered, because offering one that cannot be
built is offering an error.

## Deploying

```bash
scripts/pull-fly.sh   # first: keep the logs and records the deploy would lose
fly deploy
fly logs
```

The deploy does not cut traffic over until `/healthz` returns 200, which it does
only once the greeting is synthesised and the reasoning connection is open.

## Checking it

```bash
curl -fsS https://voice-agent-chapters.fly.dev/healthz   # ok
fly status                                      # one machine, passing
fly ssh console -C 'ls /data/sessions /data/traces'
```

Then open the URL in a browser and actually talk to it. `uv run verify` and a
container that starts prove nothing about whether the voice arrives in one
piece over a real network (AGENTS.md §6).

## The caps

`limits.py` explains what each one bounds and why a public address needs it.
They are set in `fly.toml` and are **off everywhere else**, so a local run is
still the agent of chapters 1-13:

| Variable | Deployed | Bounds |
| --- | --- | --- |
| `VOICE_AGENT_MAX_LIVE` | 4 | conversations held open at once |
| `VOICE_AGENT_SESSION_BUDGET` | 1800 | seconds before a conversation ends itself (30 min) |
| `VOICE_AGENT_MINTS_PER_IP` | 10 | new conversations per address per 10 minutes |
| `VOICE_AGENT_MAX_STORED` | 500 | conversations kept before the oldest is dropped |

To change one, edit `fly.toml` and `fly deploy` — they live with the rest of the
deployment's configuration rather than as loose secrets.

## What is written down, and how to delete it

The deployed instance **records**, and the page says so before anybody speaks.
On the 1 GB volume at `/data`:

- `/data/sessions/<date>-<id>.md` — the conversation: what was said and typed,
  the timings, the decisions, what each part cost.
- `/data/logs/voice-agent.log*` — the server's log (`VOICE_AGENT_LOGS`), INFO
  and above, UTC. Rotated at 5 MB, ten old files kept: ~55 MB at most, oldest
  dropped first. Fly's own `fly logs --no-tail` holds only ~100 lines and loses
  them on every deploy; this does not. Some warnings quote a few words of a
  reply.

The span-tree trace is **off** here (`VOICE_AGENT_TRACE = "off"`): it holds whole
prompts and replies, which AGENTS.md §10 keeps out of logs by default. Traces
written before it was turned off may remain in `/data/traces`.

**No audio is ever written.** Binary frames are counted and discarded
(Chapter 10).

Nothing expires. The delete is the one the CLI has always had:

```bash
fly ssh console            # then, on the machine:
#   uv run --no-sync voice-agent --purge-sessions   (it asks first, so not via -C)
#   rm -rf /data/traces                              (older traces, if any)
#   rm -rf /data/logs                                (the log files)
```

**Copies kept locally.** `scripts/pull-fly.sh` downloads Fly's log buffer, the
log files and every session record into `fly-archive/` (gitignored). It runs
before every deploy. A purge on the machine does not reach that copy: delete
`fly-archive/` too.

To run the public instance without recording anything, set
`VOICE_AGENT_SESSIONS` to `off` in `fly.toml` as well. The
page's notice follows the setting, so it cannot claim one while doing the other.

## Running the image locally

Worth doing before every deploy: it is the only check that catches the system
prompt failing to resolve, which happens at the first request rather than at
build time.

```bash
docker build -t voice-agent .
docker run --rm -p 8000:8000 --env-file .env voice-agent
curl -fsS localhost:8000/healthz
```
