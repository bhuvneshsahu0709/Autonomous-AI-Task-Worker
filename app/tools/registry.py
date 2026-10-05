"""Tool registry.

The registry is what makes the worker and the verifier two configurations of one
engine rather than two programs. Same executor, same loop, same error handling -
they differ only in which tools they are handed.
"""

from __future__ import annotations

from app.tools.base import ToolSpec
from app.tools.browser import BROWSER_TOOLS
from app.tools.control import FINISH_TOOL, VERDICT_TOOL
from app.tools.files import FILE_TOOLS
from app.tools.human import HUMAN_TOOLS
from app.tools.memory_tools import MEMORY_TOOLS
from app.tools.web_http import HTTP_TOOLS


class Toolbelt:
    def __init__(self, specs: list[ToolSpec]) -> None:
        self._by_name = {s.name: s for s in specs}

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def __len__(self) -> int:
        return len(self._by_name)

    @property
    def names(self) -> list[str]:
        return list(self._by_name)

    def get(self, name: str) -> ToolSpec | None:
        return self._by_name.get(name)

    def specs(self) -> list[ToolSpec]:
        return list(self._by_name.values())

    def anthropic_schemas(self) -> list[dict]:
        return [s.anthropic_schema() for s in self._by_name.values()]


def _all_specs() -> list[ToolSpec]:
    return [
        *BROWSER_TOOLS,
        *HTTP_TOOLS,
        *FILE_TOOLS,
        *MEMORY_TOOLS,
        *HUMAN_TOOLS,
        FINISH_TOOL,
    ]


def worker_toolbelt(*, allow_human: bool = True) -> Toolbelt:
    """Everything the worker can do."""
    specs = _all_specs()
    if not allow_human:
        human = {s.name for s in HUMAN_TOOLS}
        specs = [s for s in specs if s.name not in human]
    return Toolbelt(specs)


def verifier_toolbelt() -> Toolbelt:
    """Read-only observation plus the verdict tool.

    Built by *filtering on the read_only flag*, not by listing names by hand, so
    a new tool added later cannot accidentally leak write access into
    verification - it has to opt in by being read-only.
    """
    specs = [s for s in _all_specs() if s.read_only and s.verifier_safe]
    return Toolbelt([*specs, VERDICT_TOOL])
