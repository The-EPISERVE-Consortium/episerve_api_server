"""Errors shared by the benchmark and leaderboard actions."""


class NotFound(LookupError):
    """The benchmark, entry, run or dataset does not exist (or is not available for this action)."""


class Conflict(Exception):
    """The request conflicts with the current state (for example the run is already on the board)."""


class NotDraft(Conflict):
    """The benchmark is final or retired and cannot be changed."""
