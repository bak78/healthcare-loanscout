"""The checks that keep bad leads out of the results: an inbox listed in an
earlier batch, a department's inbox, a national chain's location, and a
neighbour's Google listing at the same address."""

from pathlib import Path

from healthcare_loanscout import cli, leads
from healthcare_loanscout.contacts import is_wrong_department
from healthcare_loanscout.export import CsvWriter, remembered_emails
from healthcare_loanscout.places import match_listing
from test_cleanup import CountingFinder
from test_email_finder import StubFinder
from test_run import TODAY, StubPlaces, crawl_of, health, listing, org  # noqa: F401

# --- emails remembered across batches -----------------------------------------------------------


def test_an_email_is_remembered_after_its_batch_is_deleted(tmp_path):
    out = Path(tmp_path) / "leads.csv"
    with CsvWriter(out) as writer:
        writer.write({"key": "111", "email": "Jane@UnityHH.com"})
        writer.write({"key": "222", "email": ""})
    out.unlink()  # a fresh batch

    assert remembered_emails(out) == {"jane@unityhh.com": "111"}
    assert cli.emails_in(str(out)) == {"jane@unityhh.com"}


def test_starting_over_forgets_the_emails_too(tmp_path):
    out = Path(tmp_path) / "leads.csv"
    with CsvWriter(out) as writer:
        writer.write({"key": "111", "email": "jane@unityhh.com"})
    with CsvWriter(out, resume=False):
        pass
    assert remembered_emails(out) == {}


def test_a_preview_remembers_nothing(tmp_path):
    out = Path(tmp_path) / "leads_preview.csv"
    with CsvWriter(out, resume=False, track_seen=False) as writer:
        writer.write({"key": "111", "email": "jane@unityhh.com"})
    assert remembered_emails(out) == {}


# --- department inboxes ---------------------------------------------------------------------------


def test_department_inboxes():
    for email in ("hr@a.com", "careers@a.com", "jobs@a.com", "recruiting@a.com", "billing@a.com",
                  "patientbilling@a.com", "medical.records@a.com", "medicalrecordsdept@a.com",
                  "compliance@a.com", "privacy@a.com", "webmaster@a.com", "press@a.com",
                  "hr.team@a.com", "store1234@a.com", "public.affairs@a.com"):
        assert is_wrong_department(email), email
    for email in ("info@a.com", "office@a.com", "jane@a.com", "hrobinson@a.com", "presley@a.com",
                  "mediaz@a.com", "jobrien@a.com", "billy@a.com"):
        assert not is_wrong_department(email), email


def test_a_department_inbox_is_passed_over_for_the_front_desk(rules, health, monkeypatch):
    site = "<p>Apply: careers@unityhh.com</p><p>Questions? info@unityhh.com</p>"
    monkeypatch.setattr(leads, "crawl_site", lambda *a, **k: crawl_of(("https://unityhh.com/", site)))
    lead = leads.build_lead(org(health), StubPlaces(), object(), rules, health, 8, TODAY)
    assert lead.email == "info@unityhh.com"


def test_only_a_department_inbox_counts_as_none_so_the_finder_is_asked(rules, health, monkeypatch):
    finder = StubFinder()
    monkeypatch.setattr(leads, "crawl_site",
                        lambda *a, **k: crawl_of(("https://unityhh.com/", "<p>hr@unityhh.com</p>")))
    lead = leads.build_lead(org(health), StubPlaces(), object(), rules, health, 8, TODAY, email_finder=finder)
    assert finder.asked and lead.email == "jobrien@unityhh.com"


# --- national chains ------------------------------------------------------------------------------


def test_a_national_chains_location_is_left_out_without_a_paid_search(rules, health, monkeypatch):
    finder = CountingFinder()
    monkeypatch.setattr(leads, "crawl_site", lambda *a, **k: crawl_of(("https://www.cvs.com/", "<p>Hi</p>")))
    places = StubPlaces(listing(website="https://www.cvs.com/minuteclinic/clinic-locator/pa/philadelphia"))
    lead = leads.build_lead(org(health), places, object(), rules, health, 8, TODAY, email_finder=finder)
    assert lead.score.disqualified and lead.score.disqualify_reason == "national_chain"
    assert finder.asked == []


def test_a_small_practice_is_not_a_national_chain(rules, health, monkeypatch):
    monkeypatch.setattr(leads, "crawl_site", lambda *a, **k: crawl_of(("https://unityhh.com/", "<p>Hi</p>")))
    lead = leads.build_lead(org(health), StubPlaces(), object(), rules, health, 8, TODAY)
    assert not lead.score.disqualified


# --- a neighbour's listing at the same address ----------------------------------------------------


def place(title, phone="(215) 555-0134"):
    return {"title": title, "address": "1234 Market St, Philadelphia, PA 19107",
            "phoneNumber": phone, "website": "https://example.com/"}


def test_another_business_at_the_same_address_is_not_a_match():
    found = match_listing([place("Bright Smiles Dental")], "Unity Home Health", "1234 MARKET ST STE 200", "19107")
    assert found.matched_on == ""
    assert found.note == "the only listing at this address is another business (Bright Smiles Dental)"


def test_an_address_match_with_a_shared_name_word_is_a_match():
    found = match_listing([place("Unity Homecare")], "Unity Home Health", "1234 MARKET ST", "19107")
    assert found.matched_on == "address"


def test_names_written_together_or_apart_still_match():
    found = match_listing([place("Northstar Physical Therapy")], "North Star PT LLC", "1234 MARKET ST", "19107")
    assert found.matched_on == "address"


def test_the_legal_name_or_officials_name_can_confirm_it():
    listing_ = place("Jane O'Brien, DMD")
    assert match_listing([listing_], "Bright Smiles PC", "1234 MARKET ST", "19107").matched_on == ""
    assert match_listing([listing_], "Bright Smiles PC", "1234 MARKET ST", "19107",
                         names=("Jane O'Brien",)).matched_on == "address"


def test_the_registry_phone_can_confirm_it():
    found = match_listing([place("Main Line Wellness", "(215) 555-0134")], "Unity Home Health",
                          "1234 MARKET ST", "19107", phones=("215-555-0134",))
    assert found.matched_on == "address + phone", "labelled: renamed or sold, the contact may be out of date"


def test_a_shared_toll_free_number_proves_nothing():
    found = match_listing([place("Main Line Wellness", "(800) 555-0134")], "Unity Home Health",
                          "1234 MARKET ST", "19107", phones=("800-555-0134",))
    assert found.matched_on == ""


def test_the_right_listing_is_found_past_a_neighbour():
    found = match_listing([place("Bright Smiles Dental"), place("Unity Home Health Care")],
                          "Unity Home Health", "1234 MARKET ST", "19107")
    assert (found.matched_on, found.name) == ("address", "Unity Home Health Care")
