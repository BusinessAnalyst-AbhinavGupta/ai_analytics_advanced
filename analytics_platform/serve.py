"""Streamlit-free API for the standalone analytics platform (PI/P2)."""
import os

from .api import main, create_app


def make_serve(settings=None):
    """Create the app and, when ANALYTICS_WATCHER=1, start the Phase 9 scheduler."""
    app = create_app(settings)
    if settings is not None and settings.scheduler_enabled or \
            os.environ.get("ANALYTICS_WATCHER") == "1":
        ctx = app.state.ctx
        if ctx.scheduler is not None:
            from .scheduler import Scheduler
            if isinstance(ctx.scheduler, Scheduler):
                ctx.scheduler.start()
    return app


__all__ = ["main", "create_app", "make_serve"]