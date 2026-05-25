from __future__ import annotations

from app.scheduling.simple import SimpleScheduler


class FSRSScheduler(SimpleScheduler):
    """Placeholder adapter.

    The app depends on the Scheduler interface, so py-fsrs can replace this
    without changing review routes. The MVP uses the simple scheduler to keep
    deployment small and predictable.
    """

