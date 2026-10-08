"""Find the authorized official's email with Hunter.io's Email Finder.

The NPI registry names the official and Google gives the website, which is
exactly what Email Finder takes: a first name, a last name and a domain. It
returns the most likely address and whether Hunter could verify the mailbox.
Only verified addresses are kept. "accept_all" (the mail server says yes to
every address, so nothing can be confirmed) and "unknown" are left out — a
guessed address is worse than none.

https://hunter.io/api-documentation/v2#email-finder
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

log = logging.getLogger("healthcare_loanscout")

HUNTER_URL = "https://api.hunter.io/v2"
TIMEOUT = 20


class HunterUnavailable(RuntimeError):
    """Hunter can't be used at all (bad key, out of searches); stop the run."""


@dataclass(frozen=True)
class HunterResult:
    email: str = ""  # '' unless Hunter verified the mailbox
    status: str = ""  # Hunter's verification status: valid, accept_all, unknown, invalid
    score: int = 0  # Hunter's confidence, 0-100
    position: str = ""  # job title Hunter has for the person, when it has one


class HunterClient:
    """Hunter's Email Finder. A search costs one Hunter credit when it returns
    an address, verified or not; one that finds nothing is free (measured
    October 2026: 16 searches, 5 credits)."""

    def __init__(self, api_key: str, session=None):
        self._key = api_key
        self._session = session or requests.Session()
        self.lookups = 0
        self.found = 0
        # Once Hunter refuses the key or runs out of searches, every other
        # worker's next search would be refused too, so they fail fast.
        self._refused: HunterUnavailable | None = None

    def find(self, domain: str, first: str, last: str) -> HunterResult:
        if self._refused is not None:
            raise self._refused
        params = {"domain": domain, "first_name": first, "last_name": last, "api_key": self._key}
        try:
            response = self._session.get(f"{HUNTER_URL}/email-finder", params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            # A network blip on one organization is not a reason to stop the run.
            raise ConnectionError(f"Hunter request failed: {type(exc).__name__}") from exc
        self.lookups += 1

        if response.status_code == 401:
            self._refused = HunterUnavailable("Hunter rejected the API key. Check HUNTER_API_KEY in .env")
            raise self._refused
        if response.status_code in (402, 429) or (response.status_code == 403 and "usage" in response.text):
            self._refused = HunterUnavailable(
                "Hunter is out of searches for this month — upgrade at hunter.io, "
                "or remove HUNTER_API_KEY from .env to run without it")
            raise self._refused
        if response.status_code in (400, 404, 422, 451):
            # A domain or name Hunter can't search (or a person who opted out).
            return HunterResult()
        if response.status_code != 200:
            raise ConnectionError(f"Hunter returned {response.status_code}")

        data = response.json().get("data") or {}
        email = (data.get("email") or "").strip().lower()
        status = ((data.get("verification") or {}).get("status") or "").lower()
        result = HunterResult(email=email if status == "valid" else "", status=status,
                              score=int(data.get("score") or 0), position=data.get("position") or "")
        if result.email:
            self.found += 1
        elif email:
            log.info("Hunter: %s %s at %s is probably %s, but Hunter couldn't verify it (%s) — not used",
                     first, last, domain, email, status or "no status")
        return result

    def close(self) -> None:
        self._session.close()
