"""Portboard: local registry of projects, their dev-server instances and the ports they hold.

Keep this module free of imports: ``python3 -m unittest`` discovery imports the
``portboard`` package *before* ``tests/``, and ``config`` pins ``DB_PATH`` to
``~/.local/state/portboard`` the moment it is imported. An eager import here
made the whole suite run against the real database once (2026-09-08).
"""


def __getattr__(name: str):
    if name == "__version__":
        from .config import VERSION
        return VERSION
    raise AttributeError(name)
