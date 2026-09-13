"""A local dashboard over TaskSpindle task and worker state.

Task data is always opened ``mode=ro`` (see :mod:`.db`), and the dashboard exposes no mutation API.
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
