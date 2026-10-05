"""Secret redaction for everything durable.

The boundary this draws is the important bit:

* The **model** sees secrets. It has to - it cannot sign in to the portal
  without the password it just read out of the workspace.
* The **record** does not. Nothing written to ``run.json``, ``report.md``, the
  console event stream or a step observation keeps the plaintext.

So redaction is applied on the way *out* of the agent (to disk and to the
operator), never on the way *in* (to the model). Doing it the other way round
would be security theatre that also breaks the task.

Values are learned rather than configured: whatever the agent reads out of a
credentials file, or types into a password field, is registered and scrubbed
from then on. That generalises to credentials this prototype has never seen.
"""

from __future__ import annotations

import re

# `Password: `Nw!nd-2026``, `password = hunter2`, `api_key: "abc"` ...
_SECRET_LINE_RE = re.compile(
    r"""(?im)^\s*[-*]?\s*
        (?:password|passwd|secret|api[_\s-]?key|token|credential)
        \s*[:=]\s*
        [`"']?(?P<value>[^\s`"'<>]{4,})[`"']?\s*$
    """,
    re.VERBOSE,
)

MASK = "[redacted]"
MIN_LENGTH = 4


class SecretRegistry:
    """Per-run set of values that must never be persisted in plaintext."""

    def __init__(self) -> None:
        self._values: set[str] = set()

    def __len__(self) -> int:
        return len(self._values)

    def add(self, value: str) -> None:
        value = (value or "").strip()
        if len(value) >= MIN_LENGTH:
            self._values.add(value)

    def learn_from(self, text: str) -> int:
        """Register anything in `text` that looks like a declared credential."""
        before = len(self._values)
        for match in _SECRET_LINE_RE.finditer(text or ""):
            self.add(match.group("value"))
        return len(self._values) - before

    def scrub(self, text: str) -> str:
        if not text or not self._values:
            return text
        # Longest first, so overlapping secrets mask cleanly.
        for value in sorted(self._values, key=len, reverse=True):
            text = text.replace(value, MASK)
        return text

    def scrub_args(self, args: dict) -> dict:
        return {
            key: self.scrub(value) if isinstance(value, str) else value
            for key, value in args.items()
        }
