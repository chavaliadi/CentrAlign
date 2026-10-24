"""Tool provisioning, kept out of the controller.

The controller used to import `BrowserSession`, `register_browser_tools`,
`register_file_tools` and `register_human_tools` and install them on every run.
That made the README's claim that `loop.py` holds no browser/PDF/payables
knowledge false, and it made the generalisation story weaker than advertised:
adding a non-browser capability meant editing the controller, not just adding a
file.

So provisioning lives here and is injected. `loop.py` now depends only on
`tools.base` - a registry of named callables returning Observations - and has
no idea what those capabilities are. Swapping the whole toolset (a desktop
driver, an API-only set, stubs for a test) is a different factory passed to
`TaskWorker`, with no change to the control loop at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .tools.base import ToolRegistry


@dataclass
class ToolContext:
    """Everything a toolset may need from the run, with no tool specifics."""

    downloads_dir: Path
    screenshots_dir: Path
    # Record an established fact in working memory, with provenance.
    remember: Callable[[str, Any, str, str], None]
    # Put a question to the human and return their answer.
    ask_human: Callable[[str, dict[str, Any]], str]


@dataclass
class Toolset:
    registry: ToolRegistry
    teardown: Callable[[], None]


class ToolsetFactory(Protocol):
    def __call__(self, context: ToolContext) -> Toolset: ...


def browser_toolset(context: ToolContext) -> Toolset:
    """The default: a real browser, local file reading, and human-in-the-loop.

    Imported lazily so a run that uses a different toolset - or a test that
    stubs one - never needs Playwright installed.
    """
    from .tools.browser import BrowserSession, register_browser_tools
    from .tools.files import register_file_tools
    from .tools.human import register_human_tools

    session = BrowserSession(
        downloads_dir=context.downloads_dir,
        screenshots_dir=context.screenshots_dir,
    )
    session.start()

    registry = ToolRegistry()
    register_browser_tools(registry, session)
    register_file_tools(registry)
    register_human_tools(registry, context.ask_human, context.remember)

    return Toolset(registry=registry, teardown=session.stop)


def readonly_toolset(context: ToolContext) -> Toolset:
    """Files and human interaction only, no browser.

    Exists to demonstrate the point rather than because a task needs it: the
    controller runs unchanged against a toolset that cannot browse at all.
    """
    from .tools.files import register_file_tools
    from .tools.human import register_human_tools

    registry = ToolRegistry()
    register_file_tools(registry)
    register_human_tools(registry, context.ask_human, context.remember)
    return Toolset(registry=registry, teardown=lambda: None)
