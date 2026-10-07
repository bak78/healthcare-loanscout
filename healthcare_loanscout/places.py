"""Find a healthcare organization's Google Maps listing via Serper's Places search.

A listing carries the phone number and website the business chose to publish,
plus its Google rating and review count — the public signs of how busy it is.
Search results are only trusted when they are provably the same business: the
listing's street address must match the NPI practice address, or failing that,
its name must match and it must sit in the same ZIP code. Anything less is
left blank, because calling the wrong business is worse than calling none.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import requests

SERPER_PLACES_URL = "https://google.serper.dev/places"
TIMEOUT = 15

# Words that carry no identity in a street or business name.
_STREET_WORDS = {
    "n", "s", "e", "w", "north", "south", "east", "west",
    "st", "street", "ave", "avenue", "rd", "road", "blvd", "boulevard", "dr",
    "drive", "ln", "lane", "pl", "place", "ct", "court", "ter", "terrace",
    "pike", "hwy", "highway", "pkwy", "parkway", "way", "sq", "square",
}
# Generic healthcare words are left out too: "Unity Home Health Care LLC" and
# "Unity Homecare" share "unity", which is what identifies them.
_BUSINESS_WORDS = {
    "llc", "inc", "corp", "co", "company", "ltd", "lp", "pc", "pllc", "the", "and", "of",
    "home", "health", "healthcare", "care", "homecare", "services", "service", "medical",
    "group", "center", "centre", "agency", "associates", "clinic", "practice",
}


class SerperUnavailable(RuntimeError):
    """Serper can't be used at all (bad key, out of credits); stop the run."""


@dataclass(frozen=True)
class Listing:
    """The matched Google listing, or an empty one when nothing matched."""

    name: str = ""
    address: str = ""
    phone: str = ""
    website: str = ""
    matched_on: str = ""  # "address", "name + zip", or "" for no match
    note: str = ""
    rating: float = 0.0  # Google star rating, 0 when none
    reviews: int = 0  # number of Google ratings
    category: str = ""  # Google's category, e.g. "Home health care service"


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower().replace("&", " and "))


def _street_numbers(address: str) -> set[str]:
    """'1528-30 SANSOM ST' -> {'1528', '1529', '1530'}. City addresses write
    a building spanning several lots as a range with a shortened end."""
    match = re.match(r"\s*(\d+)(?:-(\d+))?", address or "")
    if not match:
        return set()
    start, end = match.group(1), match.group(2)
    if not end:
        return {start}
    end = start[: len(start) - len(end)] + end
    low, high = int(start), int(end)
    if high < low or high - low > 60:
        return {start}
    return {str(n) for n in range(low, high + 1)}


# Where the street ends and the unit begins: '1234 MARKET ST STE 200'.
_UNIT = re.compile(
    r"(?:,?\s(?:ste|suite|unit|apt|apartment|fl|floor|rm|room|bldg|building|dept)\b|\s*#).*$",
    re.IGNORECASE,
)


def _street_core(address: str) -> set[str]:
    """The identifying words of a street: '903 N 2ND ST' -> {'2nd'}.

    Unit designators are cut off first. NPI addresses carry them
    ('1234 MARKET ST STE 200') and Google often omits them, so leaving
    'ste' and '200' in would make an exact listing fail to match."""
    street = _UNIT.sub("", address or "")
    words = _words(re.sub(r"^\s*[\d-]+\s*", "", street))
    return {w for w in words if w not in _STREET_WORDS}


def _name_overlap(a: str, b: str) -> float:
    """Share of a's meaningful words that also appear in b."""
    left = {w for w in _words(a) if w not in _BUSINESS_WORDS}
    right = {w for w in _words(b) if w not in _BUSINESS_WORDS}
    if not left:
        return 0.0
    return len(left & right) / len(left)


def match_listing(places: list[dict], search_name: str, address: str, zip5: str) -> Listing:
    """Pick the listing that is provably this business, or none."""
    numbers = _street_numbers(address)
    core = _street_core(address)

    for place in places:
        place_address = place.get("address") or ""
        place_words = set(_words(place_address))
        if numbers and core and numbers & place_words and core <= place_words:
            return _listing(place, "address")

    for place in places:
        place_address = place.get("address") or ""
        if zip5 and zip5 in place_address and _name_overlap(search_name, place.get("title", "")) >= 0.6:
            return _listing(place, "name + zip")

    return Listing(note=f"no listing matched ({len(places)} checked)" if places else "no listings found")


def _listing(place: dict, matched_on: str) -> Listing:
    return Listing(
        name=place.get("title") or "",
        address=place.get("address") or "",
        phone=place.get("phoneNumber") or "",
        website=place.get("website") or "",
        matched_on=matched_on,
        rating=_number(place.get("rating"), float),
        reviews=_number(place.get("ratingCount"), int),
        category=place.get("category") or "",
    )


def _number(value, kind):
    """Serper sends numbers, occasionally as strings; anything else is 0."""
    try:
        return kind(value or 0)
    except (TypeError, ValueError):
        return kind(0)


class PlacesClient:
    """Serper Places search. One lookup costs one Serper credit."""

    def __init__(self, api_key: str):
        self._session = requests.Session()
        self._session.headers.update(
            {"X-API-KEY": api_key, "Content-Type": "application/json"}
        )
        self.lookups = 0
        # Once Serper refuses the key or runs out of credits, every other
        # worker's next lookup would be refused too, so they fail fast.
        self._refused: SerperUnavailable | None = None

    def search(self, query: str) -> list[dict]:
        if self._refused is not None:
            raise self._refused
        try:
            response = self._session.post(
                SERPER_PLACES_URL, json={"q": query, "gl": "us"}, timeout=TIMEOUT
            )
        except requests.RequestException as exc:
            # A network blip on one business is not a reason to stop the run.
            raise ConnectionError(f"Serper request failed: {exc}") from exc
        self.lookups += 1

        if response.status_code in (401, 403):
            self._refused = SerperUnavailable(
                "Serper rejected the API key. Check SERPER_API_KEY in .env"
            )
            raise self._refused
        if response.status_code == 429:
            self._refused = SerperUnavailable(
                "Serper is out of credits or rate-limited (429)"
            )
            raise self._refused
        if response.status_code != 200:
            raise ConnectionError(f"Serper returned {response.status_code}")
        return response.json().get("places") or []

    def find(self, search_name: str, address: str, city: str, state: str, zip5: str) -> Listing:
        query = f"{search_name}, {address}, {city}, {state} {zip5}".strip()
        return match_listing(self.search(query), search_name, address, zip5)

    def close(self) -> None:
        self._session.close()
