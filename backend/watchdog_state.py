"""Shared watchdog state for the server entry point and HTTP routes."""

# The server may run as __main__ while routes import backend.server. Keeping
# this flag in a canonical module makes both names see the same state.
disabled = False
