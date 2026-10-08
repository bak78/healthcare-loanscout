"""Leads, CSV rows, and whole runs — with the NPI registry, Serper, and websites stubbed."""

import csv
import threading
from datetime import date

import pytest

from healthcare_loanscout import cli, leads
from healthcare_loanscout.crawl import SiteCrawl
from healthcare_loanscout.export import COLUMNS, build_row
from healthcare_loanscout.extract import build_page
from healthcare_loanscout.npi import NpiClient, load_health_rules, parse_record
from healthcare_loanscout.places import Listing, SerperUnavailable
from test_npi import FakeSession, record

TODAY = date(2026, 10, 7)


@pytest.fixture(scope="module")
def health():
    return load_health_rules()


def org(health, **overrides):
    return parse_record(record(**overrides), health, None)


def listing(**overrides):
    values = dict(name="Unity Home Health", address="1234 Market St, Philadelphia, PA 19107",
                  phone="+1 215-555-0177", website="https://unityhh.com/", matched_on="address",
                  rating=4.7, reviews=212, category="Home health care service")
    values.update(overrides)
    return Listing(**values)


class StubPlaces:
    def __init__(self, result=None, error=None):
        self.result = result or listing()
        self.error = error
        self.lookups = 0
        self.queries = []

    def find(self, *args):
        self.lookups += 1
        self.queries.append(args)
        if self.error:
            raise self.error
        return self.result

    def close(self):
        pass


def crawl_of(*pages):
    """A finished SiteCrawl from (url, html) pairs."""
    crawl = SiteCrawl(domain="unityhh.com", start_url="https://unityhh.com/")
    crawl.pages = [build_page(url, html) for url, html in pages]
    return crawl


# --- scoring ----------------------------------------------------------------------------


def test_npi_facts_score_without_any_lookup(rules, health):
    lead = leads.build_lead(org(health), None, None, rules, health, 8, TODAY)
    found = set(lead.score.signal_ids)
    assert {"npi_registered", "npi_established", "official_owner"} <= found
    assert lead.score.total > 0
    assert "Jane O'Brien, President/CEO" in " ".join(h.snippet for h in lead.score.hits)


def test_google_facts_from_serper_raise_the_weight(rules, health):
    preview = leads.build_lead(org(health), None, None, rules, health, 8, TODAY)
    looked_up = leads.build_lead(org(health), StubPlaces(), None, rules, health, 8, TODAY)
    assert {"google_listing", "google_reviews_many", "google_good_rating"} <= set(looked_up.score.signal_ids)
    assert looked_up.score.total > preview.score.total
    assert "4.7 stars from 212 ratings" in " ".join(h.snippet for h in looked_up.score.hits)


def test_owner_outweighs_office_staff(rules, health):
    owner = leads.build_lead(org(health), None, None, rules, health, 8, TODAY)
    staff = leads.build_lead(org(health, basic={"authorized_official_title_or_position": "Billing Clerk"}),
                             None, None, rules, health, 8, TODAY)
    assert owner.score.total > staff.score.total


def test_few_reviews_get_less_weight_than_many(rules, health):
    few = leads.build_lead(org(health), StubPlaces(listing(reviews=12, rating=3.2)), None,
                           rules, health, 8, TODAY)
    assert "google_reviews_some" in few.score.signal_ids
    assert "google_good_rating" not in few.score.signal_ids


def test_lookup_uses_the_public_name_and_practice_address(rules, health):
    places = StubPlaces()
    leads.build_lead(org(health), places, None, rules, health, 8, TODAY)
    assert places.queries[0] == ("1 Unity Homehealth", "1234 Market St Ste 200", "Philadelphia", "PA", "19107")


def test_no_listing_is_flagged_as_possibly_closed(rules, health):
    gone = StubPlaces(Listing(note="no listings found"))
    row = build_row(leads.build_lead(org(health), gone, None, rules, health, 8, TODAY), rules)
    assert "may have closed" in row["notes"]
    assert row["phone"] == "(215) 555-0134", "falls back to the NPI practice phone"
    assert row["phone_source"] == "NPI registry"


# --- email ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email,belongs",
    [
        ("jane.obrien@unityhh.com", True),
        ("jobrien@unityhh.com", True),
        ("jane@unityhh.com", True),
        ("info@unityhh.com", False),
        ("office@unityhh.com", False),
        ("mark@unityhh.com", False),
    ],
)
def test_email_belongs_to_the_official(email, belongs):
    assert leads.email_belongs_to(email, "Jane", "O'Brien") is belongs


def test_short_surname_must_sit_at_an_end():
    assert leads.email_belongs_to("leej@x.com", "Jo", "Lee")
    assert not leads.email_belongs_to("fleet@x.com", "Jo", "Lee")


def test_the_officials_own_email_beats_the_front_desk(health):
    crawl = crawl_of(
        ("https://unityhh.com/", '<a href="mailto:info@unityhh.com">Email us</a>'),
        ("https://unityhh.com/about", "<p>Contact our president at jane.obrien@unityhh.com</p>"),
    )
    email, page, is_official = leads.pick_email(crawl, org(health))
    assert (email, page, is_official) == ("jane.obrien@unityhh.com", "https://unityhh.com/about", True)


def test_without_the_officials_email_the_sites_best_is_used(health):
    crawl = crawl_of(("https://unityhh.com/contact", '<a href="mailto:info@unityhh.com">Email</a>'))
    assert leads.pick_email(crawl, org(health)) == ("info@unityhh.com", "https://unityhh.com/contact", False)


# --- rows ----------------------------------------------------------------------------------------


def test_row_leads_with_the_requested_columns(rules, health):
    assert COLUMNS[:10] == ["first_name", "last_name", "title", "decision_maker", "email", "phone",
                            "company_name", "healthcare_industry", "specialty", "score"]
    row = build_row(leads.build_lead(org(health), StubPlaces(), None, rules, health, 8, TODAY), rules)
    assert (row["first_name"], row["last_name"]) == ("Jane", "O'Brien")
    assert row["company_name"] == "1 Unity Homehealth LLC"
    assert (row["healthcare_industry"], row["specialty"]) == ("Home care", "Home Health")
    assert row["phone"] == "(215) 555-0177" and row["phone_source"] == "Google listing"
    assert row["official_phone"] == "(215) 555-0199"
    assert (row["google_rating"], row["google_reviews"]) == ("4.7", 212)
    assert row["decision_maker"] == "owner / president"
    assert set(row) == set(COLUMNS)


# --- whole runs ----------------------------------------------------------------------------------


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    monkeypatch.setenv("HUNTER_API_KEY", "")  # never spend real Hunter credits in tests
    # The crawler is never reached: listings carry no website. Each
    # organization's site gives it its own email, as a real Run would.
    monkeypatch.setattr(leads, "pick_email", lambda crawl, o, checker=None: (f"office{o.npi}@gmail.com", "", False))
    return tmp_path


def _client(count=3):
    return NpiClient(FakeSession({("Home Health", "191*"): count}), cache_dir=None, pause=0)


def _rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_preview_is_free_and_writes_every_organization(workdir):
    places = StubPlaces()
    options = cli.RunOptions(fields=("Home care",), zip_prefixes=("191",), dry_run=True,
                             out="out/leads.csv")
    assert cli.run_leads(options, npi_client=_client(5), places=places) == cli.EXIT_OK
    assert places.lookups == 0
    assert len(_rows(workdir / "out/leads_preview.csv")) == 5


def test_run_looks_up_the_limit_and_resumes_where_it_stopped(workdir):
    places = StubPlaces(listing(website=""))
    options = cli.RunOptions(fields=("Home care",), zip_prefixes=("191",), limit=2,
                             out="out/leads.csv", workers=2)
    assert cli.run_leads(options, npi_client=_client(3), places=places) == cli.EXIT_OK
    assert places.lookups == 2
    assert len(_rows(workdir / "out/leads.csv")) == 2

    assert cli.run_leads(options, npi_client=_client(3), places=places) == cli.EXIT_OK
    assert places.lookups == 3, "the second run only looked up the one left"
    assert len(_rows(workdir / "out/leads.csv")) == 3

    assert cli.run_leads(options, npi_client=_client(3), places=places) == cli.EXIT_DEPLETED


def test_rows_are_sorted_by_score(workdir):
    options = cli.RunOptions(fields=("Home care",), zip_prefixes=("191",), limit=0,
                             out="out/leads.csv", workers=1)
    cli.run_leads(options, npi_client=_client(4), places=StubPlaces(listing(website="")))
    scores = [float(r["score"]) for r in _rows(workdir / "out/leads.csv")]
    assert scores == sorted(scores, reverse=True)


def test_out_of_credits_stops_the_run(workdir):
    places = StubPlaces(error=SerperUnavailable("Serper is out of credits"))
    options = cli.RunOptions(fields=("Home care",), zip_prefixes=("191",), out="out/leads.csv")
    assert cli.run_leads(options, npi_client=_client(), places=places) == cli.EXIT_SERPER_UNAVAILABLE


def test_one_failed_lookup_is_retried_next_run(workdir):
    places = StubPlaces(error=ConnectionError("network blip"))
    options = cli.RunOptions(fields=("Home care",), zip_prefixes=("191",), out="out/leads.csv")
    assert cli.run_leads(options, npi_client=_client(1), places=places) == cli.EXIT_LOOKUPS_FAILED

    places.error = None
    places.result = listing(website="")
    assert cli.run_leads(options, npi_client=_client(1), places=places) == cli.EXIT_OK
    assert len(_rows(workdir / "out/leads.csv")) == 1


def test_nothing_found_is_its_own_outcome(workdir):
    options = cli.RunOptions(fields=("Home care",), zip_prefixes=("191",), dry_run=True, out="out/leads.csv")
    assert cli.run_leads(options, npi_client=_client(0), places=StubPlaces()) == cli.EXIT_NO_MATCHES


def test_unknown_field_and_bad_zip_stop_before_searching(workdir):
    session = FakeSession({})
    client = NpiClient(session, cache_dir=None, pause=0)
    bad_field = cli.RunOptions(fields=("Veterinary",), dry_run=True)
    bad_zip = cli.RunOptions(zip_prefixes=("1",), dry_run=True)
    assert cli.run_leads(bad_field, npi_client=client) == cli.EXIT_CONFIG_ERROR
    assert cli.run_leads(bad_zip, npi_client=client) == cli.EXIT_CONFIG_ERROR
    assert session.calls == []


def test_run_without_a_key_explains_how_to_add_one(workdir, monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "")
    monkeypatch.setattr("healthcare_loanscout.config.load_dotenv", lambda *a, **k: None)
    options = cli.RunOptions(fields=("Home care",), zip_prefixes=("191",), out="out/leads.csv")
    assert cli.run_leads(options, npi_client=_client()) == cli.EXIT_CONFIG_ERROR


def test_stop_during_lookups_keeps_finished_rows(workdir):
    stop = threading.Event()

    class SlowPlaces(StubPlaces):
        def find(self, *args):
            stop.set()  # pressing Stop while the first lookups are in flight
            return super().find(*args)

    options = cli.RunOptions(fields=("Home care",), zip_prefixes=("191",), limit=0,
                             out="out/leads.csv", workers=1)
    result = cli.run_leads(options, stop, npi_client=_client(20), places=SlowPlaces(listing(website="")))
    assert result == cli.EXIT_INTERRUPTED
    assert len(_rows(workdir / "out/leads.csv")) < 20


# --- only leads with a working email ----------------------------------------------------------


def test_organizations_without_a_working_email_are_left_out_and_not_looked_up_again(workdir, monkeypatch):
    # Two organizations share an inbox; the third's website has no email.
    def pick(crawl, o, checker=None):
        return ("", "", False) if o.company_name.endswith("2") else ("office@gmail.com", "", False)
    monkeypatch.setattr(leads, "pick_email", pick)
    places = StubPlaces(listing(website=""))
    options = cli.RunOptions(fields=("Home care",), zip_prefixes=("191",), limit=0,
                             out="out/leads.csv", workers=1)
    assert cli.run_leads(options, npi_client=_client(3), places=places) == cli.EXIT_OK
    assert [r["email"] for r in _rows(workdir / "out/leads.csv")] == ["office@gmail.com"], \
        "one had no email, one repeated an inbox already listed"
    assert cli.run_leads(options, npi_client=_client(3), places=places) == cli.EXIT_DEPLETED
    assert places.lookups == 3, "the ones left out aren't paid for twice"


def test_a_website_email_on_someone_elses_domain_is_not_theirs(health):
    crawl = crawl_of(("https://unityhh.com/", "<footer>Site by design@webshop.com</footer>"))
    assert leads.pick_email(crawl, org(health)) == ("", "", False)


def test_the_officials_own_address_is_picked_first(health):
    crawl = crawl_of(("https://unityhh.com/contact", "<p>info@unityhh.com</p>"),
                     ("https://unityhh.com/team", "<p>jobrien@unityhh.com</p>"))
    assert leads.pick_email(crawl, org(health)) == ("jobrien@unityhh.com", "https://unityhh.com/team", True)
