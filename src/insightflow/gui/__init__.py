"""Desktop operator shell (tkinter) for the InsightFlow pipeline.

Stage 0 ships the window skeleton, the background-worker contract and the
locked-environment banner.  Business actions arrive in later stages and are
wired through ``insightflow.gui.services`` so this package never re-implements
pipeline logic.
"""

from __future__ import annotations

from .app import InsightFlowApp
from .envcheck import EnvStatus, check_environment
from .worker import BackgroundWorker, Message

__all__ = ["InsightFlowApp", "EnvStatus", "check_environment", "BackgroundWorker", "Message"]


def launch() -> int:
    """Start the window from Python (the .bat launcher uses ``-m`` instead)."""
    from .__main__ import main

    return main()
