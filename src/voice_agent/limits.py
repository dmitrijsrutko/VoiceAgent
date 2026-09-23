"""The caps a public address needs, and nothing more.

Every chapter before this one ran on localhost, where the only person who could
open a conversation was the person who started the server. A public URL removes
that, and the exposure is larger than "a stranger reads the page": `GET /` mints
a conversation on every load, an open socket starts the initiative clock, and
each rung of a silence is a billed reasoning call. A crawler that follows one
link can therefore spend money at a rate nobody chose, without ever saying a
word.

Everything here is **off unless configured**, and the deployment is what turns
it on (`fly.toml`). A development run that quietly throttled itself would make
chapters 1-13 behave differently depending on where they ran, which is the kind
of divergence between the local thing and the deployed thing that this project
refuses everywhere else.

None of this is security. It is a spend ceiling and a fair-use rule held in one
process's memory, and anyone who minds can defeat it by changing their address.
What it is built to stop is the accident: the crawler, the tab left open
overnight, the demo that goes around a classroom.
"""

import time
from collections.abc import Mapping

MINT_WINDOW = 600.0
"""The span over which `MintLimit`'s allowance refills, in seconds.

Not configurable. The allowance is the knob worth having — how many
conversations one address may start — and a second number that changes what the
first one means would only make the pair harder to reason about.
"""

PRUNE_AT = 1024
"""How many addresses `MintLimit` tracks before it clears out the spent ones."""


class Live:
    """How many conversations may be held open at once.

    Counted on the socket rather than on the page, because holding a socket is
    what costs: the recognizer's stream, the reasoning connection, and the clock
    that considers speaking into a silence all belong to a connection, and none
    of them to a page that was merely loaded.
    """

    def __init__(self, cap: int | None) -> None:
        self._cap = cap
        self.held = 0

    def take(self) -> bool:
        """Claim a slot, or refuse. Safe without a lock: the event loop cannot
        switch tasks between the test and the increment, there being no await."""
        if self._cap is not None and self.held >= self._cap:
            return False
        self.held += 1
        return True

    def release(self) -> None:
        self.held = max(0, self.held - 1)


class MintLimit:
    """How many conversations one address may start, refilled continuously.

    A token bucket rather than a fixed window. A window lets twice the allowance
    through across its boundary, which for a limit this small is most of the
    limit — ten per ten minutes would admit twenty in the wrong two.
    """

    def __init__(self, allowance: int | None, window: float = MINT_WINDOW) -> None:
        self._allowance = allowance
        self._window = window
        self._spent: dict[str, tuple[float, float]] = {}
        """address -> (tokens left, when that was true)."""

    def allow(self, address: str, now: float | None = None) -> bool:
        if self._allowance is None:
            return True
        moment = time.monotonic() if now is None else now
        full = float(self._allowance)
        tokens, since = self._spent.get(address, (full, moment))
        tokens = min(full, tokens + (moment - since) * full / self._window)
        if tokens < 1.0:
            # Nothing is written on a refusal, which is what makes the refill
            # exact. Re-recording `(tokens, moment)` here instead — the obvious
            # thing — recomputes the balance from its own last estimate, and a
            # caller retrying every second adds a tenth a hundred times: the
            # accumulated float error left a one-per-ten-seconds bucket at
            # 0.999… after ten seconds, refusing forever. From an untouched
            # `since` it is one subtraction and one multiply, however often it
            # is asked.
            return False
        self._prune(moment)
        self._spent[address] = (tokens - 1.0, moment)
        return True

    def _prune(self, moment: float) -> None:
        """A bucket a whole window old has refilled to its allowance, so it says
        exactly what no entry at all says, and can be forgotten."""
        if len(self._spent) <= PRUNE_AT:
            return
        self._spent = {
            address: spend
            for address, spend in self._spent.items()
            if moment - spend[1] < self._window
        }


def client_address(headers: Mapping[str, str], peer: str | None) -> str:
    """Who to count a request against, from behind whatever is in front of us.

    `Fly-Client-IP` first, because that is where this deploys and Fly sets it
    itself. The first entry of `X-Forwarded-For` next, for any other proxy.
    The socket's own peer last — the only one of the three nobody can choose,
    and the right answer on localhost, where there is no proxy to ask.

    A header is exactly as honest as whatever set it, so counting on one is
    forgeable by anyone reaching the server directly. Behind Fly's proxy nobody
    is, and the cap is a spend ceiling rather than a defence in any case.
    """
    fly = headers.get("fly-client-ip", "").strip()
    if fly:
        return fly
    forwarded = headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    if first:
        return first
    return peer or "unknown"


def budget_reason(seconds: float) -> str:
    """What the page is told when a conversation runs out of time.

    Phrased as a demo's house rule rather than as a failure, because that is
    what it is — and it names the length, so nobody is left wondering whether
    the agent hung up on them.
    """
    minutes = seconds / 60
    length = f"{minutes:g} minutes" if minutes >= 1 else f"{seconds:g} seconds"
    return f"This public demo limits a conversation to {length}. Reload to start a new one."
