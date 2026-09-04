"""A read-only web dashboard over the TaskSpindle database.

Everything here only reads: the database is opened ``mode=ro`` (see :mod:`.db`), and every route
in :mod:`.app` is GET-only. There is no authentication -- binding anywhere but localhost is the
caller's decision, made in :mod:`taskspindle.cli`, not here.
"""

from __future__ import annotations

import threading
import webbrowser
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Paths
    from ..providers import Profile

__all__ = ["serve"]


def serve(
    paths: Paths,
    profiles: dict[str, Profile],
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> int:
    """Build the dashboard app and serve it until interrupted; returns the process exit code."""
    import uvicorn

    from .app import build_app

    app = build_app(paths, profiles)
    url = f"http://{host}:{port}"
    print(f"taskspindle web: {url}")
    if open_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0
