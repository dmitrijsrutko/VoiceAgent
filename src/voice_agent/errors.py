"""Errors raised by the voice agent itself, independent of any vendor SDK."""


class VoiceAgentError(Exception):
    """Base class for every error this project raises deliberately."""


class ConfigError(VoiceAgentError):
    """The environment is missing or misconfigured (e.g. no API key)."""


class ProviderError(VoiceAgentError):
    """A reasoning-engine backend failed.

    Vendor SDK exceptions are translated into this at the adapter boundary so
    nothing above `providers/` has to know which SDK is in use.
    """


class SessionNotFoundError(VoiceAgentError):
    """The requested conversation key is not in the in-memory store."""
