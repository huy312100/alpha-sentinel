"""Ops scripts: the measurements an operator needs and a soak does not produce.

Kept deliberately separate from ``bot`` (operator CLI) and ``ops`` (the supervisor):
neither is a service, and a one-off that prints a table should not be imported by
the daemon that has to make decisions.
"""
