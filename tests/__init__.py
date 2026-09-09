"""Test package guard: keep every test away from ~/.local/state/portboard.

``config`` resolves ``DB_PATH`` at import time, so the env var must be set
before the first ``from portboard import ...`` anywhere. This module is
imported by unittest discovery before any ``test_*.py`` and does two things:
it sets a per-run default state dir, and it aborts loudly if ``portboard.config``
was somehow imported earlier pointing at the real home (that happened on
2026-09-08 and wiped the user's registry).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PORTBOARD_STATE_DIR", tempfile.mkdtemp(prefix="portboard-tests-"))

_REAL_STATE_DIR = Path("~/.local/state/portboard").expanduser().resolve()


def assert_isolated() -> None:
    """Raise unless ``portboard.config`` points at a throwaway state dir.

    Call it before any test that writes or deletes rows through ``db.connect()``
    with the default path.
    """
    config = sys.modules.get("portboard.config")
    if config is None:
        return
    for attr in ("STATE_DIR", "DB_PATH", "LOG_PATH"):
        value = Path(getattr(config, attr)).expanduser()
        try:
            resolved = value.resolve()
        except OSError:
            resolved = value
        if resolved == _REAL_STATE_DIR or _REAL_STATE_DIR in resolved.parents:
            raise RuntimeError(
                f"portboard.config.{attr} = {value} is the real state dir; "
                "PORTBOARD_STATE_DIR was set too late for this process — refusing to run tests"
            )


assert_isolated()
