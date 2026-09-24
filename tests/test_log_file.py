"""The log kept on disk: Fly's own buffer is ~100 lines and is gone after a
deploy, so the public instance writes its log to the volume as well."""

import logging
import re
import tomllib
from pathlib import Path

import pytest

from voice_agent.cli import LOG_FILE_BYTES, LOG_FILES_KEPT, open_log_file
from voice_agent.config import load_settings


def test_off_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOICE_AGENT_LOGS", raising=False)

    assert load_settings().logs is None
    assert open_log_file(None) is None


def test_the_public_instance_writes_its_log_to_the_volume() -> None:
    fly = tomllib.loads((Path(__file__).parents[1] / "fly.toml").read_text(encoding="utf-8"))

    assert fly["env"]["VOICE_AGENT_LOGS"].startswith("/data/")


def test_info_and_above_are_kept_with_utc_times_and_a_size_cap(tmp_path: Path) -> None:
    handler = open_log_file(tmp_path / "logs")
    assert handler is not None
    logger = logging.getLogger("voice_agent.test_log_file")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        logger.debug("not kept")
        logger.info("a microphone was refused")
        logger.warning("synthesis unfinished")
    finally:
        logger.removeHandler(handler)
        handler.close()

    text = (tmp_path / "logs" / "voice-agent.log").read_text(encoding="utf-8")
    assert "not kept" not in text
    assert re.search(
        r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ INFO voice_agent\.test_log_file: a microphone",
        text,
        re.M,
    )
    assert "WARNING voice_agent.test_log_file: synthesis unfinished" in text
    assert handler.maxBytes == LOG_FILE_BYTES and handler.backupCount == LOG_FILES_KEPT  # type: ignore[attr-defined]
