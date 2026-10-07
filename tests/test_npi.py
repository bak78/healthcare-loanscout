"""NPI registry: settings, record parsing, grouping, and searching — the API stubbed."""

import pytest

from healthcare_loanscout.config import ConfigError
from healthcare_loanscout.npi import (
    SEARCH_CAP, NpiClient, NpiUnavailable, SearchStopped, check_zip_prefixes,
    find_organizations, group_organizations, load_health_rules, parse_record, tidy_name,
)


@pytest.fixture(scope="module")
def health():
    return load_health_rules()


def record(**overrides):
    """A registry record shaped like the real API's, for a Philadelphia home care agency."""
    basic = {
        "organization_name": "1 UNITY HOMEHEALTH LLC",
        "organizational_subpart": "NO",
        "status": "A",
        "enumeration_date": "2015-03-02",
        "authorized_official_first_name": "JANE",
        "authorized_official_last_name": "O'BRIEN",
        "authorized_official_middle_name": "Q",
        "authorized_official_title_or_position": "President/CEO",
        "authorized_official_telephone_number": "2155550199",
        "authorized_official_name_suffix": "--",
    }
    basic.update(overrides.pop("basic", {}))
    rec = {
        "number": overrides.pop("number", "1234567890"),
        "basic": basic,
        "addresses": [
            {"address_purpose": "MAILING", "address_1": "PO BOX 1", "city": "BALA CYNWYD",
             "state": "PA", "postal_code": "190040001", "telephone_number": "610-555-0000"},
            {"address_purpose": "LOCATION", "address_1": "1234 MARKET ST", "address_2": "STE 200",
             "city": "PHILADELPHIA", "state": "PA", "postal_code": "191071234",
             "telephone_number": "215-555-0134"},
        ],
        "other_names": [],
        "taxonomies": [
            {"desc": "Clinic/Center, Mental Health", "primary": False},
            {"desc": "Home Health", "primary": True},
        ],
    }
    rec.update(overrides)
    return rec


# --- settings ------------------------------------------------------------------


def test_fields_load_in_order_with_home_care_first(health):
    assert health.field_names[0] == "Home care"
    assert "Dental" in health.field_names
    assert health.field_named("home CARE").name == "Home care"


def test_unknown_field_names_the_choices(health):
    with pytest.raises(ConfigError, match="Home care"):
        health.field_named("Veterinary")


@pytest.mark.parametrize(
    "specialty,field",
    [
        ("Home Health", "Home care"),
        ("In Home Supportive Care", "Home care"),
        ("Technician, Personal Care Attendant", "Home care"),
        ("Hospice Care, Community Based", "Hospice"),
        ("Clinic/Center, Adult Day Care", "Adult day care"),
        ("Counselor, Mental Health", "Behavioral health"),
        ("Dentist, General Practice", "Dental"),
        ("Pharmacy, Community/Retail Pharmacy", "Pharmacy"),
        ("Ambulance, Land Transport", "Medical transport"),
        ("Clinic/Center, Urgent Care", "Primary & urgent care"),
        ("Emergency Medicine", None),
    ],
)
def test_primary_specialty_decides_the_field(health, specialty, field):
    found = health.classify(specialty)
    assert (found.name if found else None) == field


@pytest.mark.parametrize(
    "title,level",
    [
        ("President/CEO", "owner"),
        ("PRESIDENT/C.E.O", "owner"),
        ("Owner/Executive Director", "owner"),
        ("Managing Member", "owner"),
        ("Dentist", "clinician"),
        ("Administrator", "manager"),
        ("Executive Director", "manager"),
        ("Office Manager", "staff"),
        ("Billing Specialist", "staff"),
        ("", "unknown"),
        ("Grand Poobah", "unknown"),
    ],
)
def test_titles_sort_into_decision_levels(health, title, level):
    assert health.title_level(title) == level


@pytest.mark.parametrize("prefix", ["1", "191x", "123456", ""])
def test_zip_prefixes_need_two_to_five_digits(prefix):
    with pytest.raises(ConfigError, match="2 to 5 digits"):
        check_zip_prefixes((prefix,))


# --- names -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,tidy",
    [
        ("1 UNITY HOMEHEALTH LLC", "1 Unity Homehealth LLC"),
        ("DON'S HOME CARE, INC.", "Don's Home Care, Inc."),
        ("O'BRIEN", "O'Brien"),
        ("YEUNG-TIEU", "Yeung-Tieu"),
        ("ROBERT A LEVINE DDS PC", "Robert A Levine DDS PC"),
        ("DE LA CRUZ", "De La Cruz"),
        ("21ST CENTURY HOME CARE", "21st Century Home Care"),
        ("AT HOME 4 CARE", "At Home 4 Care"),
        ("McDonald Dental", "McDonald Dental"),
    ],
)
def test_names_are_tidied_for_people_to_read(raw, tidy):
    assert tidy_name(raw) == tidy


# --- parsing --------------------------------------------------------------------------


def test_a_record_becomes_an_organization_with_its_official(health):
    org = parse_record(record(), health, None)
    assert (org.official_first, org.official_last) == ("Jane", "O'Brien")
    assert org.official_title == "President/CEO"
    assert org.official_level == "owner"
    assert org.official_phone == "(215) 555-0199"
    assert org.legal_name == "1 Unity Homehealth LLC"
    assert org.field == "Home care"
    assert org.specialty == "Home Health"


def test_the_practice_address_is_used_not_the_mailing_address(health):
    org = parse_record(record(), health, None)
    assert (org.address, org.city, org.zip) == ("1234 Market St Ste 200", "Philadelphia", "19107")
    assert org.phone == "(215) 555-0134"


def test_dba_is_the_public_name_and_drives_the_google_search(health):
    rec = record(other_names=[{"code": "3", "type": "Doing Business As",
                               "organization_name": "UNITY CARE PARTNERS"}])
    org = parse_record(rec, health, None)
    assert org.company_name == "Unity Care Partners"
    assert org.search_name == "Unity Care Partners"
    assert parse_record(record(), health, None).search_name == "1 Unity Homehealth"


def test_a_dba_that_repeats_the_legal_name_is_ignored(health):
    rec = record(other_names=[{"code": "3", "organization_name": "1 UNITY HOMEHEALTH, LLC"}])
    assert parse_record(rec, health, None).dba == ""


@pytest.mark.parametrize(
    "overrides",
    [
        {"basic": {"status": "D"}},
        {"basic": {"organizational_subpart": "YES"}},
        {"basic": {"organization_name": "TEMPLE UNIVERSITY HOSPITAL INC"}},
        {"basic": {"organization_name": "CITY OF PHILADELPHIA"}},
        {"taxonomies": [{"desc": "General Acute Care Hospital", "primary": True}]},
    ],
)
def test_hospitals_public_bodies_subparts_and_inactive_are_not_leads(health, overrides):
    assert parse_record(record(**overrides), health, None) is None


def test_secondary_specialty_match_takes_the_primary_specialtys_field(health):
    rec = record(taxonomies=[{"desc": "Home Health", "primary": False},
                             {"desc": "Skilled Nursing Facility", "primary": True}])
    assert parse_record(rec, health, health.field_named("Home care")).field == "Nursing & assisted living"


def test_unlisted_primary_specialty_keeps_the_field_it_was_found_under(health):
    rec = record(taxonomies=[{"desc": "Case Management", "primary": True}])
    assert parse_record(rec, health, health.field_named("Home care")).field == "Home care"
    assert parse_record(rec, health, None) is None


def test_no_official_name_means_unknown_level(health):
    rec = record(basic={"authorized_official_first_name": "", "authorized_official_last_name": "--"})
    org = parse_record(rec, health, None)
    assert (org.official_first, org.official_last, org.official_level) == ("", "", "unknown")


def test_years_registered_counts_whole_years(health):
    from datetime import date
    org = parse_record(record(), health, None)
    assert org.years_registered(date(2026, 3, 1)) == 10
    assert org.years_registered(date(2026, 3, 2)) == 11


def test_same_name_at_several_addresses_is_one_lead_with_locations(health):
    first = parse_record(record(number="1", basic={"enumeration_date": "2019-01-01"}), health, None)
    older = parse_record(record(number="2", basic={"enumeration_date": "2012-06-01"}), health, None)
    elsewhere = parse_record(record(number="3", addresses=[
        {"address_purpose": "LOCATION", "address_1": "9 OTHER RD", "city": "PHILADELPHIA",
         "state": "PA", "postal_code": "19120"}]), health, None)
    [lead] = group_organizations([first, older, elsewhere])
    assert lead.npi == "2", "the oldest registration speaks for the organization"
    assert lead.locations == 2, "two NPIs at one address are one location"


# --- searching ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        return self.payload


class FakeSession:
    """Answers registry searches from a table: (taxonomy, postal_code) -> count."""

    def __init__(self, counts, errors=None, status=200):
        self.counts = counts
        self.errors = errors
        self.status = status
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append(dict(params))
        if self.status != 200:
            return FakeResponse({}, self.status)
        if self.errors:
            return FakeResponse({"Errors": [{"description": self.errors}]})
        total = self.counts.get((params["taxonomy_description"], params.get("postal_code")), 0)
        start = params["skip"]
        size = max(0, min(params["limit"], total - start))
        # The real registry never returns past skip 1,000.
        if start > 1000:
            size = 0
        zip_base = (params.get("postal_code") or "19").rstrip("*").ljust(5, "0")
        return FakeResponse({"results": [
            record(number=f"{params['taxonomy_description'][:3]}{params.get('postal_code')}-{start + i}",
                   basic={"organization_name": f"AGENCY {params.get('postal_code')} {start + i}"},
                   addresses=[{"address_purpose": "LOCATION", "address_1": f"{start + i} MAIN ST",
                               "city": "PHILADELPHIA", "state": "PA", "postal_code": zip_base}])
            for i in range(size)
        ]})

    def close(self):
        pass


def test_search_pages_until_a_short_page(health):
    session = FakeSession({("Home Health", "191*"): 450})
    results, capped = NpiClient(session, cache_dir=None, pause=0).search("Home Health", "PA", "191")
    assert len(results) == 450 and not capped
    assert [c["skip"] for c in session.calls] == [0, 200, 400]


def test_a_full_search_is_split_into_narrower_zip_prefixes(health):
    counts = {("Dentist", "191*"): 5000}
    counts.update({("Dentist", f"191{d}*"): 30 for d in range(10)})
    session = FakeSession(counts)
    found = NpiClient(session, cache_dir=None, pause=0).collect("Dentist", "PA", "191")
    assert len(found) == 300
    assert {c.get("postal_code") for c in session.calls} >= {"1910*", "1919*"}


def test_whole_state_search_splits_into_two_digit_prefixes(health):
    counts = {("Dentist", None): SEARCH_CAP, ("Dentist", "19*"): 7}
    session = FakeSession(counts)
    found = NpiClient(session, cache_dir=None, pause=0).collect("Dentist", "PA", None)
    assert len(found) == 7
    assert "00*" in {c.get("postal_code") for c in session.calls}


def test_results_are_cached_between_runs(health, tmp_path):
    session = FakeSession({("Home Health", "191*"): 3})
    NpiClient(session, cache_dir=tmp_path, pause=0).search("Home Health", "PA", "191")
    again = FakeSession({})
    results, _ = NpiClient(again, cache_dir=tmp_path, pause=0).search("Home Health", "PA", "191")
    assert len(results) == 3 and again.calls == []


def test_a_settings_typo_is_reported_with_the_name(health):
    session = FakeSession({}, errors="No taxonomy codes found with entered description")
    with pytest.raises(NpiUnavailable, match="Hoem Health"):
        NpiClient(session, cache_dir=None, pause=0).search("Hoem Health", "PA", "191")


def test_an_unreachable_registry_is_reported(health, monkeypatch):
    monkeypatch.setattr("healthcare_loanscout.npi.RETRIES", 1)
    with pytest.raises(NpiUnavailable, match="internet"):
        NpiClient(FakeSession({}, status=503), cache_dir=None, pause=0).search("Home Health", "PA", "191")


def test_stop_ends_a_search(health):
    import threading
    stop = threading.Event()
    stop.set()
    with pytest.raises(SearchStopped):
        NpiClient(FakeSession({}), cache_dir=None, stop=stop).search("Home Health", "PA", "191")


def test_find_keeps_only_the_chosen_field_and_area(health):
    session = FakeSession({("Home Health", "191*"): 4})
    orgs = find_organizations(NpiClient(session, cache_dir=None, pause=0), health, "PA", ("191",),
                              ("Home care",))
    assert len(orgs) == 4
    assert {o.field for o in orgs} == {"Home care"}
    searched = {c["taxonomy_description"] for c in session.calls}
    assert searched == set(health.field_named("Home care").taxonomies)


def test_find_drops_organizations_practicing_outside_the_area(health):
    class Elsewhere(FakeSession):
        def get(self, url, params, timeout):
            response = super().get(url, params, timeout)
            for rec in response.payload.get("results", []):
                rec["addresses"][0]["postal_code"] = "08101"  # mailing matched, practice in NJ
                rec["addresses"][0]["state"] = "NJ"
            return response

    session = Elsewhere({("Home Health", "191*"): 2})
    assert find_organizations(NpiClient(session, cache_dir=None, pause=0), health, "PA", ("191",),
                              ("Home care",)) == []
