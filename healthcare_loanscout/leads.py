"""Turn one NPI organization into a scored lead.

NPI facts (who the official is, how long it's been registered, how many
locations) and Google facts found through Serper (a matching listing, its
rating and review count) become signal hits of their own, scored by the same
engine as website text. Every point of a score is traceable to evidence: a
row can say "fit 62 — Google: 4.7 stars from 212 ratings" next to the name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .config import Rules
from .contacts import is_role_address
from .crawl import SiteCrawl, crawl_site, is_crawlable
from .emailcheck import FREE_MAIL, EmailChecker, domain_of
from .fetch import PoliteFetcher
from .npi import HealthRules, Organization
from .places import Listing, PlacesClient
from .score import Score, score_site
from .signals import SignalHit, match_site

NPI_SOURCE = "NPI registry"
GOOGLE_SOURCE = "Google listing (Serper)"


@dataclass(frozen=True)
class Lead:
    org: Organization
    listing: Listing
    crawl: SiteCrawl | None
    score: Score
    email: str = ""
    email_page: str = ""
    email_is_official: bool = False


def _hit(signal_id: str, weight: float, snippet: str, source: str) -> SignalHit:
    return SignalHit(signal_id=signal_id, category="fit", weight=float(weight),
                     snippet=snippet, source_url=source, page_type="registry")


def npi_hits(org: Organization, health: HealthRules, today: date | None = None) -> list[SignalHit]:
    """What the NPI record says about this organization."""
    w = health.weights
    hits = [_hit("npi_registered", w.get("npi_registered", 8),
                 f"{org.field}: {org.specialty} (NPI {org.npi})", NPI_SOURCE)]

    years = org.years_registered(today)
    if years >= int(w.get("established_years", 3)):
        hits.append(_hit("npi_established", w.get("established", 10),
                         f"NPI registered {org.enumerated} ({years} years)", NPI_SOURCE))

    level_weight = (w.get("decision_maker") or {}).get(org.official_level, 0)
    if level_weight:
        name = f"{org.official_first} {org.official_last}".strip()
        hits.append(_hit(f"official_{org.official_level}", level_weight,
                         f"Authorized official: {name}, {org.official_title or 'no title given'}",
                         NPI_SOURCE))

    if org.locations >= 2:
        hits.append(_hit("multi_location", w.get("multi_location", 10),
                         f"{org.locations} practice locations in the NPI registry", NPI_SOURCE))
    return hits


def google_hits(listing: Listing, health: HealthRules) -> list[SignalHit]:
    """What the matched Google listing says: open, and how busy."""
    if not listing.matched_on:
        return []
    w = health.weights
    hits = [_hit("google_listing", w.get("google_listing", 10),
                 f"Google listing: {listing.name} — {listing.address} (matched on {listing.matched_on})",
                 GOOGLE_SOURCE)]
    stars = f"{listing.rating:.1f} stars from {listing.reviews} ratings"
    if listing.reviews >= 50:
        hits.append(_hit("google_reviews_many", w.get("reviews_many", 14), f"Google: {stars}", GOOGLE_SOURCE))
    elif listing.reviews >= 10:
        hits.append(_hit("google_reviews_some", w.get("reviews_some", 7), f"Google: {stars}", GOOGLE_SOURCE))
    if listing.reviews >= 10 and listing.rating >= 4.0:
        hits.append(_hit("google_good_rating", w.get("good_rating", 5), f"Google: {stars}", GOOGLE_SOURCE))
    return hits


def _letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", (text or "").lower())


def email_belongs_to(email: str, first: str, last: str) -> bool:
    """True when the address's mailbox carries the official's name:
    jane.doe@, jdoe@, doej@, jane@ — but never info@ or office@."""
    if not email or is_role_address(email):
        return False
    local = _letters(email.partition("@")[0])
    first, last = _letters(first), _letters(last)
    # A short surname must sit at an end, or "Lee" would match fleet@.
    if len(last) >= 4 and last in local:
        return True
    if len(last) == 3 and (local.startswith(last) or local.endswith(last)):
        return True
    return len(first) >= 3 and local.startswith(first)


def email_is_theirs(email: str, site_domain: str) -> bool:
    """An address on the business's own domain, or a gmail-style mailbox it
    lists on its own site — not a web designer's or a directory's."""
    domain = domain_of(email)
    if site_domain and (domain == site_domain or domain.endswith("." + site_domain)):
        return True
    return domain in FREE_MAIL


def pick_email(crawl: SiteCrawl | None, org: Organization,
               checker: EmailChecker | None = None) -> tuple[str, str, bool]:
    """(email, page it was on, whether it is the official's own address).

    The official's own mailbox beats the site's general best pick: it reaches
    the decision maker instead of the front desk. Only the business's own
    addresses count, and, given a checker, only ones that pass it."""
    if crawl is None or not crawl.pages:
        return "", "", False

    def usable(email: str) -> bool:
        return email_is_theirs(email, crawl.domain) and (checker is None or checker.ok(email))

    for page in crawl.pages:
        for email in page.contacts.emails:
            if email_belongs_to(email, org.official_first, org.official_last) and usable(email):
                return email, page.url, True
    contacts = crawl.contacts
    for email in (contacts.email,) + contacts.other_emails:
        if email and usable(email):
            page = next((p.url for p in crawl.pages if email in p.contacts.emails), contacts.email_page)
            return email, page, False
    return "", "", False


def build_lead(
    org: Organization,
    places: PlacesClient | None,
    fetcher: PoliteFetcher | None,
    rules: Rules,
    health: HealthRules,
    max_pages: int,
    today: date | None = None,
    email_checker: EmailChecker | None = None,
) -> Lead:
    """Look the organization up on Google, crawl its website, and score it.

    Pass places=None for a free preview: NPI facts only, no lookups.
    Raises SerperUnavailable (stop the run) or ConnectionError (retry this
    organization next run) from the lookup.
    """
    if places is None:
        listing = Listing(note="preview: not looked up")
    else:
        listing = places.find(org.search_name, org.address, org.city, org.state, org.zip)
        if not listing.matched_on:
            listing = Listing(note=f"{listing.note}; may have closed or moved")

    crawl = None
    if fetcher is not None and listing.website and is_crawlable(listing.website, rules):
        crawl = crawl_site(listing.website, fetcher, rules, max_pages)

    email, email_page, is_official = pick_email(crawl, org, email_checker)

    hits = npi_hits(org, health, today) + google_hits(listing, health)
    if email:
        w = health.weights
        hits.append(_hit("email_found", w.get("email_found", 6), f"Email on website: {email}", email_page))
        if is_official:
            hits.append(_hit("email_is_official", w.get("email_matches_official", 4),
                             f"{email} carries the official's name", email_page))
    if crawl is not None and crawl.pages:
        hits += match_site(crawl.pages, rules)

    return Lead(org, listing, crawl, score_site(hits, rules), email, email_page, is_official)
