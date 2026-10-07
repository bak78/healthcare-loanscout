from healthcare_loanscout.signals import match_page, match_site


def ids(hits):
    return {h.signal_id for h in hits}


def test_growth_signals_fire_on_expanding_business(page_factory, rules):
    hits = match_page(page_factory("growing_hvac.html"), rules)
    found = ids(hits)
    assert "new_location" in found
    assert "expansion_language" in found
    assert "hiring_active" in found
    assert "capacity_strain" in found
    assert "new_equipment" in found


def test_fit_signals_fire_on_established_local_business(page_factory, rules):
    hits = match_page(page_factory("growing_hvac.html"), rules)
    found = ids(hits)
    assert "established_business" in found
    assert "has_staff" in found


def test_repeated_phrase_counts_once(page_factory, rules):
    """Invariant 1: distinct signal ids, not occurrences."""
    hits = match_page(page_factory("repetitive_hiring.html"), rules)
    hiring = [h for h in hits if h.signal_id == "hiring_active"]
    assert len(hiring) == 1


def test_negation_suppresses_distress_signals(page_factory, rules):
    """Invariant 2: 'we are NOT going out of business' is not distress."""
    hits = match_page(page_factory("negated_closing.html"), rules)
    found = ids(hits)
    assert "closing_business" not in found
    assert "liquidation" not in found
    # the non-negated fit signal still lands
    assert "established_business" in found


def test_evidence_snippet_contains_the_matched_phrase(page_factory, rules):
    hits = match_page(page_factory("growing_hvac.html"), rules)
    hit = next(h for h in hits if h.signal_id == "new_location")
    assert "second location" in hit.snippet.lower()
    assert hit.source_url == "https://example.com/"


def test_page_bonus_applies_on_matching_page_type(page_factory, rules):
    home = page_factory("repetitive_hiring.html", "https://example.com/")
    careers = page_factory("repetitive_hiring.html", "https://example.com/careers")

    assert careers.page_type == "careers"

    home_hit = next(h for h in match_page(home, rules) if h.signal_id == "hiring_active")
    careers_hit = next(
        h for h in match_page(careers, rules) if h.signal_id == "hiring_active"
    )
    assert careers_hit.weight > home_hit.weight


def test_match_site_keeps_highest_weight_hit_per_signal(page_factory, rules):
    home = page_factory("repetitive_hiring.html", "https://example.com/")
    careers = page_factory("repetitive_hiring.html", "https://example.com/careers")

    hits = match_site([home, careers], rules)
    hiring = [h for h in hits if h.signal_id == "hiring_active"]

    assert len(hiring) == 1
    # the careers-page hit won, because the page bonus made it heavier
    assert hiring[0].source_url.endswith("/careers")


def test_site_hits_are_sorted_by_weight_descending(page_factory, rules):
    hits = match_site([page_factory("growing_hvac.html")], rules)
    weights = [h.weight for h in hits]
    assert weights == sorted(weights, reverse=True)


def test_disqualifier_fires_on_lender(page_factory, rules):
    hits = match_page(page_factory("bank_disqualify.html"), rules)
    assert "is_lender" in ids(hits)
