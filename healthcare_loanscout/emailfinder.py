"""What an email finder (Anymailfinder) gives back.

An email finder takes a person's first and last name and their company's
domain and returns the most likely address, and whether the service could
verify the mailbox. Only verified addresses are used; anything less is
logged and left out, because a guessed address is worse than none.
"""

from __future__ import annotations

from dataclasses import dataclass


class EmailFinderUnavailable(RuntimeError):
    """The email finder can't be used at all (bad key, out of credits); stop the run."""


@dataclass(frozen=True)
class FinderResult:
    email: str = ""  # '' unless the service verified the mailbox
    status: str = ""  # the service's own verdict: valid, risky, accept_all, not_found...
    score: int = 0  # confidence 0-100, for services that give one
    position: str = ""  # job title the service has for the person, when it has one


def describe(finder, result: FinderResult) -> str:
    """'Anymailfinder (verified)', with the service's confidence when it gives one."""
    confidence = f", confidence {result.score}" if result.score else ""
    return f"{finder.name} (verified{confidence})"
