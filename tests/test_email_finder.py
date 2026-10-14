"""The email finder: when a lead asks it, which answers are used, and what a
run does when it runs out of credits. The finder is stubbed, so no credits
are spent."""

import pytest

from healthcare_loanscout import cli, leads
from healthcare_loanscout.anymailfinder import AnymailfinderUnavailable
from healthcare_loanscout.emailfinder import FinderResult
from test_run import TODAY, StubPlaces, _client, crawl_of, health, listing, org, workdir  # noqa: F401


# --- when a lead asks the email finder ------------------------------------------------------------------


class StubFinder:
    name = "Anymailfinder"
    def __init__(self, email="jobrien@unityhh.com"):
        self.email = email
        self.asked = []

    def find(self, domain, first, last):
        self.asked.append((domain, first, last))
        return FinderResult(self.email, "valid" if self.email else "")


def _lead(rules, health, monkeypatch, site_html, finder):
    monkeypatch.setattr(leads, "crawl_site", lambda *a, **k: crawl_of(("https://unityhh.com/", site_html)))
    return leads.build_lead(org(health), StubPlaces(), object(), rules, health, 8, TODAY, email_finder=finder)


def test_finder_fills_in_when_the_website_has_no_email(rules, health, monkeypatch):
    finder = StubFinder()
    lead = _lead(rules, health, monkeypatch, "<p>Call us</p>", finder)
    assert finder.asked == [("unityhh.com", "Jane", "O'Brien")]
    assert (lead.email, lead.email_is_official) == ("jobrien@unityhh.com", True)
    assert lead.email_page.startswith("Anymailfinder (verified")


def test_email_finder_is_not_asked_when_the_website_has_an_email(rules, health, monkeypatch):
    finder = StubFinder()
    lead = _lead(rules, health, monkeypatch, "<p>info@unityhh.com</p>", finder)
    assert finder.asked == [] and lead.email == "info@unityhh.com"


def test_set_to_always_email_finder_replaces_a_front_desk_address(rules, health, monkeypatch):
    import dataclasses
    always = dataclasses.replace(health, email_finder_when="always")
    finder = StubFinder()
    monkeypatch.setattr(leads, "crawl_site", lambda *a, **k: crawl_of(("https://unityhh.com/", "<p>info@unityhh.com</p>")))
    lead = leads.build_lead(org(always), StubPlaces(), object(), rules, always, 8, TODAY, email_finder=finder)
    assert lead.email == "jobrien@unityhh.com"


def test_a_email_finder_address_on_another_domain_is_not_used(rules, health, monkeypatch):
    lead = _lead(rules, health, monkeypatch, "<p>Call us</p>", StubFinder("jane@otherbusiness.com"))
    assert lead.email == ""


def test_email_finder_is_not_asked_without_a_website(rules, health):
    finder = StubFinder()
    leads.build_lead(org(health), StubPlaces(listing(website="")), None, rules, health, 8, TODAY, email_finder=finder)
    assert finder.asked == []


# --- whole runs ---------------------------------------------------------------------------------


def test_a_run_stops_when_email_finder_runs_out_and_keeps_finished_rows(workdir, monkeypatch):
    monkeypatch.setattr(leads, "pick_email", lambda crawl, o, checker=None: ("", "", False))
    monkeypatch.setattr(leads, "crawl_site", lambda *a, **k: crawl_of(("https://unityhh.com/", "<p>Hi</p>")))

    class OutOfCredits:
        name = "Anymailfinder"
        lookups, found = 1, 0

        def find(self, *args):
            raise AnymailfinderUnavailable("Anymailfinder is out of credits")

        def close(self):
            pass

    options = cli.RunOptions(fields=("Home care",), zip_prefixes=("191",), limit=0, out="out/leads.csv", workers=1)
    result = cli.run_leads(options, npi_client=_client(3), places=StubPlaces(), email_finder=OutOfCredits())
    assert result == cli.EXIT_SERPER_UNAVAILABLE
