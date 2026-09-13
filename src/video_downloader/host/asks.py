"""Questions the core asks the front end, and what happens when nobody answers.

Two of them exist, and both predate the protocol: whether a large download
should start, and whether the user wants to sign in to a site. In-process they
were callables on the job. Across a connection they become a message with an
`askId` and a reply carrying the same one.

The part worth designing is not the happy path. It is the three ways an answer
can fail to arrive, because a job is blocked on each of them:

* **Nobody is connected.** The front end never started, or already exited. The
  ask fails immediately rather than waiting for a front end that is not coming.
* **The connection drops while the question is open.** Every pending ask fails
  at once; a job waiting on a window that is gone would otherwise wait for its
  timeout with nothing at the other end.
* **Nobody answers in time.** A login takes as long as a person takes, so the
  timeouts are generous and different per question - but they are finite,
  because a coroutine parked forever holds a provider session open with it.

In all three cases the caller gets `AskUnavailable` and decides what the
conservative answer is. That decision belongs to the caller and not here: for a
size question it is "do not start", for a login it is "the refusal stands", and
those are not the same shape of answer.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: How long a question may stay open. The size question is answered by somebody
#: looking at a dialog; the login is answered by somebody typing a password, a
#: two-factor code and possibly an e-mail challenge, so it gets minutes rather
#: than a minute. Neither is infinite: a pending ask holds a provider session.
CONFIRM_TIMEOUT_SECONDS = 300.0
LOGIN_TIMEOUT_SECONDS = 900.0


class AskUnavailable(RuntimeError):
    """Nobody answered, and the caller has to decide what that means."""


class AskRegistry:
    """The open questions, and the futures the answers land in."""

    def __init__(self, send: Callable[[dict[str, Any]], bool]) -> None:
        # `send` reports whether the message reached a connected front end.
        # False is not an error here - it is the answer to "is anybody there".
        self._send = send
        self._pending: dict[str, asyncio.Future[Any]] = {}

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def ask(
        self, message_type: str, payload: dict[str, Any], timeout: float
    ) -> Any:
        """Send a question and wait for its answer.

        Raises `AskUnavailable` when no answer can be had - never returns a
        default, because a default invented here would be indistinguishable
        from a real answer at the call site.
        """
        ask_id = uuid.uuid4().hex
        answer: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[ask_id] = answer

        message = {"type": message_type, "askId": ask_id, **payload}
        try:
            if not self._send(message):
                raise AskUnavailable(f"{message_type}: no front end is connected")
            try:
                return await asyncio.wait_for(answer, timeout)
            except asyncio.TimeoutError as error:
                raise AskUnavailable(
                    f"{message_type}: no answer within {timeout:.0f}s"
                ) from error
        finally:
            self._pending.pop(ask_id, None)

    def resolve(self, ask_id: str, value: Any) -> bool:
        """Deliver an answer. Returns whether anybody was still waiting for it.

        An unknown id is not an error: the question may have timed out while the
        answer was in flight, and reporting that as a protocol violation would
        blame the front end for losing a race it did not start.
        """
        answer = self._pending.get(ask_id)
        if answer is None or answer.done():
            logger.debug("Answer to %s arrived with nobody waiting", ask_id)
            return False
        answer.set_result(value)
        return True

    def fail_all(self, reason: str) -> None:
        """End every open question at once - the front end is gone."""
        if not self._pending:
            return
        logger.info("Failing %d open question(s): %s", len(self._pending), reason)
        for answer in list(self._pending.values()):
            if not answer.done():
                answer.set_exception(AskUnavailable(reason))
        self._pending.clear()
