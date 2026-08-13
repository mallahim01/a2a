"""The live console served at ``/ui``.

A single self-contained HTML file. It is not a backend-rendered dashboard: the
page speaks A2A to the coordinator itself, opening a streaming
``SendStreamingMessage`` call and drawing the task lifecycle as events arrive.
The browser is simply another A2A client.
"""

from pathlib import Path

UI_INDEX = Path(__file__).parent / "index.html"

__all__ = ["UI_INDEX"]
