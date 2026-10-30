"""Host-side operational tooling: things that run outside the stack.

Nothing here is imported by the bot or the dashboard. :mod:`ops.watchdog` is a
supervisor for the containers themselves, so it has to outlive them - and it has
to stay stdlib-only, because on a small VM it runs under the system interpreter
rather than the project virtualenv.
"""
