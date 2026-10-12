"""Find a person's email with Anymailfinder's Find Person Email API.

Given a first name, a last name and a company domain, Anymailfinder returns
the person's address and its status: valid (verified), risky (the mail server
accepts every address, so nothing can be confirmed), not_found or blacklisted.
Only valid addresses are kept.

It charges one credit per valid email found; risky, blacklisted and not-found
results are free, and so is repeating a search within 30 days.

https://anymailfinder.com/email-finder-api/docs/find-person-email
"""

from __future__ import annotations

import logging

import requests

from .emailfinder import EmailFinderUnavailable, FinderResult

log = logging.getLogger("healthcare_loanscout")

FIND_PERSON_URL = "https://api.anymailfinder.com/v5.1/find-email/person"
# Anymailfinder checks the mail server live and suggests waiting up to 180
# seconds. A shorter wait keeps Stop prompt; a search that runs out of time is
# retried next run, and a repeat within 30 days is free.
TIMEOUT = 60


class AnymailfinderUnavailable(EmailFinderUnavailable):
    """Anymailfinder can't be used at all (bad key, out of credits)."""


class AnymailfinderClient:
    name = "Anymailfinder"

    def __init__(self, api_key: str, session=None):
        self._session = session or requests.Session()
        self._session.headers["Authorization"] = api_key
        self.lookups = 0
        self.found = 0
        # Once Anymailfinder refuses the key or runs out of credits, every
        # other worker's next search would be refused too, so they fail fast.
        self._refused: AnymailfinderUnavailable | None = None

    def find(self, domain: str, first: str, last: str) -> FinderResult:
        if self._refused is not None:
            raise self._refused
        body = {"domain": domain, "first_name": first, "last_name": last}
        try:
            response = self._session.post(FIND_PERSON_URL, json=body, timeout=TIMEOUT)
        except requests.RequestException as exc:
            # A network blip on one organization is not a reason to stop the run.
            raise ConnectionError(f"Anymailfinder request failed: {type(exc).__name__}") from exc
        self.lookups += 1

        if response.status_code == 401:
            self._refused = AnymailfinderUnavailable(
                "Anymailfinder rejected the API key. Check ANYMAILFINDER_API_KEY in .env")
            raise self._refused
        if response.status_code == 402:
            self._refused = AnymailfinderUnavailable(
                "Anymailfinder is out of credits — add more at anymailfinder.com, "
                "or remove ANYMAILFINDER_API_KEY from .env to run without it")
            raise self._refused
        if response.status_code == 400:
            # A domain or name it can't search.
            return FinderResult()
        if response.status_code != 200:
            raise ConnectionError(f"Anymailfinder returned {response.status_code}")

        data = response.json() or {}
        status = (data.get("email_status") or "").lower()
        email = (data.get("valid_email") or data.get("email") or "").strip().lower()
        result = FinderResult(email=email if status == "valid" else "", status=status,
                              position=data.get("person_job_title") or "")
        if result.email:
            self.found += 1
        elif email:
            log.info("Anymailfinder: %s %s at %s is probably %s, but it couldn't be verified (%s) — not used",
                     first, last, domain, email, status or "no status")
        return result

    def close(self) -> None:
        self._session.close()
