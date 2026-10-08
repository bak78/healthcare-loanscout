"""Hunter.io: the client, when a lead asks it, and what a run does when it
runs out — Hunter stubbed, so no credits are spent."""

import pytest

from healthcare_loanscout import cli, leads
from healthcare_loanscout.hunter import HunterClient, HunterResult, HunterUnavailable
from test_run import TODAY, StubPlaces, _client, crawl_of, health, listing, org, workdir  # noqa: F401


class FakeResponse:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self.payload = payload or {}
        self.text = text

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append(params)
        return self.responses.pop(0)

    def close(self):
        pass


def found(email="jobrien@unityhh.com", status="valid", score=97):
    return FakeResponse(200, {"data": {"email": email, "score": score, "position": "CEO",
                                       "verification": {"status": status}}})


# --- the client --------------------------------------------------------------------------


def test_a_verified_address_is_kept():
    hunter = HunterClient("key", FakeSession(found()))
    assert hunter.find("unityhh.com", "Jane", "O'Brien") == HunterResult("jobrien@unityhh.com", "valid", 97, "CEO")
    assert (hunter.lookups, hunter.found) == (1, 1)


@pytest.mark.parametrize("status", ["accept_all", "unknown", "invalid", ""])
def test_an_unverified_address_is_not(status):
    result = HunterClient("key", FakeSession(found(status=status))).find("unityhh.com", "Jane", "O'Brien")
    assert result.email == "" and result.status == status


def test_nothing_found_is_empty():
    result = HunterClient("key", FakeSession(FakeResponse(200, {"data": {"email": None}}))).find("x.com", "A", "B")
    assert result.email == ""


def test_the_search_sends_name_and_domain():
    session = FakeSession(found())
    HunterClient("key", session).find("unityhh.com", "Jane", "O'Brien")
    assert session.calls[0] == {"domain": "unityhh.com", "first_name": "Jane", "last_name": "O'Brien",
                                "api_key": "key"}


@pytest.mark.parametrize("response,message", [
    (FakeResponse(401), "rejected the API key"),
    (FakeResponse(429), "out of searches"),
    (FakeResponse(403, text='{"errors":[{"id":"usage_limit"}]}'), "out of searches"),
])
def test_a_refused_key_or_used_up_plan_stops_every_worker(response, message):
    hunter = HunterClient("key", FakeSession(response))
    with pytest.raises(HunterUnavailable, match=message):
        hunter.find("unityhh.com", "Jane", "O'Brien")
    with pytest.raises(HunterUnavailable):
        hunter.find("other.com", "Jane", "O'Brien")  # no second request


def test_a_server_error_is_retried_next_run():
    with pytest.raises(ConnectionError):
        HunterClient("key", FakeSession(FakeResponse(503))).find("unityhh.com", "Jane", "O'Brien")


# --- when a lead asks Hunter ------------------------------------------------------------------


class StubHunter:
    def __init__(self, email="jobrien@unityhh.com"):
        self.email = email
        self.asked = []

    def find(self, domain, first, last):
        self.asked.append((domain, first, last))
        return HunterResult(self.email, "valid" if self.email else "", 95)


def _lead(rules, health, monkeypatch, site_html, hunter):
    monkeypatch.setattr(leads, "crawl_site", lambda *a, **k: crawl_of(("https://unityhh.com/", site_html)))
    return leads.build_lead(org(health), StubPlaces(), object(), rules, health, 8, TODAY, hunter=hunter)


def test_hunter_fills_in_when_the_website_has_no_email(rules, health, monkeypatch):
    hunter = StubHunter()
    lead = _lead(rules, health, monkeypatch, "<p>Call us</p>", hunter)
    assert hunter.asked == [("unityhh.com", "Jane", "O'Brien")]
    assert (lead.email, lead.email_is_official) == ("jobrien@unityhh.com", True)
    assert lead.email_page.startswith("Hunter.io (verified")


def test_hunter_is_not_asked_when_the_website_has_an_email(rules, health, monkeypatch):
    hunter = StubHunter()
    lead = _lead(rules, health, monkeypatch, "<p>info@unityhh.com</p>", hunter)
    assert hunter.asked == [] and lead.email == "info@unityhh.com"


def test_set_to_always_hunter_replaces_a_front_desk_address(rules, health, monkeypatch):
    import dataclasses
    always = dataclasses.replace(health, hunter_when="always")
    hunter = StubHunter()
    monkeypatch.setattr(leads, "crawl_site", lambda *a, **k: crawl_of(("https://unityhh.com/", "<p>info@unityhh.com</p>")))
    lead = leads.build_lead(org(always), StubPlaces(), object(), rules, always, 8, TODAY, hunter=hunter)
    assert lead.email == "jobrien@unityhh.com"


def test_a_hunter_address_on_another_domain_is_not_used(rules, health, monkeypatch):
    lead = _lead(rules, health, monkeypatch, "<p>Call us</p>", StubHunter("jane@otherbusiness.com"))
    assert lead.email == ""


def test_hunter_is_not_asked_without_a_website(rules, health):
    hunter = StubHunter()
    leads.build_lead(org(health), StubPlaces(listing(website="")), None, rules, health, 8, TODAY, hunter=hunter)
    assert hunter.asked == []


# --- whole runs ---------------------------------------------------------------------------------


def test_a_run_stops_when_hunter_runs_out_and_keeps_finished_rows(workdir, monkeypatch):
    monkeypatch.setattr(leads, "pick_email", lambda crawl, o, checker=None: ("", "", False))
    monkeypatch.setattr(leads, "crawl_site", lambda *a, **k: crawl_of(("https://unityhh.com/", "<p>Hi</p>")))

    class OutHunter:
        lookups, found = 1, 0

        def find(self, *args):
            raise HunterUnavailable("Hunter is out of searches for this month")

        def close(self):
            pass

    options = cli.RunOptions(fields=("Home care",), zip_prefixes=("191",), limit=0, out="out/leads.csv", workers=1)
    result = cli.run_leads(options, npi_client=_client(3), places=StubPlaces(), hunter=OutHunter())
    assert result == cli.EXIT_SERPER_UNAVAILABLE
