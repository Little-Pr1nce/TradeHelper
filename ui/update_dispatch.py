"""Thread-safe Flet control rebuilds for application callbacks."""
from __future__ import annotations

from collections.abc import Callable


def rebuild_on_page(root, content_factory: Callable[[], object]) -> bool:
    """Rebuild a mounted root on its page event loop.

    Analysis callbacks run in an executor thread. Flet control patches must be
    submitted to the page loop; otherwise state changes may remain invisible
    until a focus or navigation event forces another client refresh.
    """
    if root is None:
        return False
    try:
        page = root.page
    except RuntimeError:
        return False
    if page is None:
        return False

    async def apply_update():
        try:
            current_page = root.page
        except RuntimeError:
            return
        if current_page is not page:
            return
        root.content = content_factory()
        root.update()

    runner = getattr(page, "run_task", None)
    if not callable(runner):
        return False
    runner(apply_update)
    return True
