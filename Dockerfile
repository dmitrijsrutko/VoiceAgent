# The image for chapter 14: the same source tree this project has always run
# from, with its dependencies locked, bound to 0.0.0.0 instead of localhost.
#
# Deliberately not a multi-stage build that installs the package into a slim
# runtime. `config.py` resolves the system prompt at
# `Path(__file__).parents[2] / "prompts"`, which is only true while the package
# sits in `src/` beside `prompts/` — the layout of a checkout. `uv sync`'s
# default editable install keeps that true; a wheel copied into site-packages
# would not, and would fail at the first request rather than at build time.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.6 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first and the project second, so editing a module re-installs
# only the project rather than re-resolving the lock. `--no-install-project`
# is what makes that split possible: without it this layer would need `src/`,
# which is the thing we are trying to keep out of it.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY src/ src/
COPY prompts/ prompts/
RUN uv sync --locked --no-dev

ENV VOICE_AGENT_HOST=0.0.0.0 \
    VOICE_AGENT_PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# `--no-sync`: the environment is already exactly the lock file, and a sync at
# boot would be one more thing between a cold machine and a greeting.
CMD ["uv", "run", "--no-sync", "voice-agent"]
