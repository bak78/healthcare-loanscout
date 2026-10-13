"""Healthcare organizations from the NPI registry.

The NPI registry (CMS NPPES) lists every healthcare provider that bills
insurance. Each organization's record names its *authorized official* — the
person who signed the registration, usually the owner or president — with a
title and a direct phone number. That is the decision maker a loan officer
wants, so the registry is both the lead list and the contact name.

The registry's API has three limits this module works around:
  * a state alone is not a search; a specialty (taxonomy) is always given
  * one search returns at most 1,200 records (200 a page, skip up to 1,000),
    so a search that fills up is split into narrower ZIP-code prefixes
  * a ZIP wildcard needs at least two digits, and matches the mailing address
    as well as the practice address, so results are filtered again here by
    practice location
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import yaml

from .config import DATA_DIR, DEFAULT_HEALTHCARE_PATH, ConfigError
from .contacts import _format_phone

log = logging.getLogger(__name__)

SEARCH_URL = "https://npiregistry.cms.hhs.gov/api/"
PAGE_SIZE = 200
MAX_SKIP = 1000
SEARCH_CAP = MAX_SKIP + PAGE_SIZE
TIMEOUT = 30
RETRIES = 3
CACHE_DIR = DATA_DIR / "npi_cache"
CACHE_DAYS = 7
SEARCH_THREADS = 4

TITLE_LEVELS = ("staff", "owner", "clinician", "manager")


class NpiUnavailable(ConnectionError):
    """The registry couldn't be reached or refused a search."""


class SearchStopped(Exception):
    """The search was cancelled with Stop or Ctrl+C."""


# --- settings ------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    """One healthcare field: a name and the NPI specialty names it covers."""

    name: str
    taxonomies: tuple[str, ...]

    def covers(self, specialty: str) -> bool:
        lowered = (specialty or "").lower()
        return any(t.lower() in lowered for t in self.taxonomies)


@dataclass(frozen=True)
class HealthRules:
    """Everything loaded from healthcare.yaml."""

    state: str
    zip_prefixes: tuple[str, ...]
    default_limit: int
    fields: tuple[Field, ...]
    exclude_taxonomies: tuple[str, ...]
    exclude_name_words: tuple[str, ...]
    title_levels: tuple[tuple[str, tuple[str, ...]], ...]
    weights: dict = field(default_factory=dict)
    require_email: bool = True
    email_finder_when: str = "missing"
    chain_min_companies: int = 3
    franchise_domains: tuple[str, ...] = ()

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    def field_named(self, name: str) -> Field:
        for f in self.fields:
            if f.name.lower() == name.lower():
                return f
        raise ConfigError(
            f"unknown field {name!r}; healthcare.yaml lists: {', '.join(self.field_names)}"
        )

    def classify(self, specialty: str) -> Field | None:
        """The first field covering this primary specialty, or None."""
        for f in self.fields:
            if f.covers(specialty):
                return f
        return None

    def excluded(self, name: str, specialty: str) -> str:
        """Why an organization is never a lead, or '' when it may be one."""
        lowered = (specialty or "").lower()
        for word in self.exclude_taxonomies:
            if word.lower() in lowered:
                return f"specialty: {specialty}"
        for words in self.exclude_name_words:
            if re.search(rf"\b{re.escape(words.lower())}\b", (name or "").lower()):
                return f"name contains '{words}'"
        return ""

    def title_level(self, title: str) -> str:
        """owner, clinician, manager, staff — or unknown."""
        # "PRESIDENT/C.E.O" -> "president/ceo"
        text = re.sub(r"\.", "", (title or "").lower())
        if not text.strip():
            return "unknown"
        for level, words in self.title_levels:
            for word in words:
                if re.search(rf"(?<![a-z]){re.escape(word.lower())}(?![a-z])", text):
                    return level
        return "unknown"


def load_health_rules(path: Path | str = DEFAULT_HEALTHCARE_PATH) -> HealthRules:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"settings file not found: {path}")
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    state = str(raw.get("state", "PA")).upper()
    if not re.fullmatch(r"[A-Z]{2}", state):
        raise ConfigError(f"healthcare.yaml state must be a two-letter code, got {state!r}")

    prefixes = tuple(str(z) for z in raw.get("zip_prefixes") or [])
    check_zip_prefixes(prefixes)

    fields = []
    for entry in raw.get("fields") or []:
        name = str(entry.get("name") or "").strip()
        taxonomies = tuple(str(t).strip() for t in entry.get("taxonomies") or [] if str(t).strip())
        if not name or not taxonomies:
            raise ConfigError(f"each healthcare.yaml field needs a name and taxonomies, got {entry!r}")
        if name.lower() in {f.name.lower() for f in fields}:
            raise ConfigError(f"field name {name!r} is used twice in healthcare.yaml")
        fields.append(Field(name, taxonomies))
    if not fields:
        raise ConfigError("healthcare.yaml lists no fields")

    levels = raw.get("title_levels") or {}
    unknown = set(levels) - set(TITLE_LEVELS)
    if unknown:
        raise ConfigError(f"healthcare.yaml title_levels has unknown levels: {sorted(unknown)}")

    return HealthRules(
        state=state,
        zip_prefixes=prefixes,
        default_limit=int(raw.get("default_limit", 50)),
        fields=tuple(fields),
        exclude_taxonomies=tuple(str(t) for t in raw.get("exclude_taxonomies") or []),
        exclude_name_words=tuple(str(w) for w in raw.get("exclude_name_words") or []),
        title_levels=tuple(
            (level, tuple(str(w) for w in levels.get(level) or [])) for level in TITLE_LEVELS
        ),
        weights=dict(raw.get("weights") or {}),
        require_email=bool(raw.get("require_email", True)),
        email_finder_when=_email_finder_when(raw.get("email_finder_when", raw.get("hunter_when", "missing"))),
        chain_min_companies=int(raw.get("chain_min_companies", 3)),
        franchise_domains=tuple(str(d).strip().lower() for d in raw.get("franchise_domains") or []),
    )


def _email_finder_when(value) -> str:
    value = str(value).strip().lower()
    if value not in ("missing", "always"):
        raise ConfigError(f"healthcare.yaml email_finder_when must be 'missing' or 'always', got {value!r}")
    return value


def check_zip_prefixes(prefixes) -> None:
    for prefix in prefixes:
        if not re.fullmatch(r"\d{2,5}", prefix):
            raise ConfigError(
                f"ZIP code start {prefix!r} must be 2 to 5 digits (the registry needs at least two)"
            )


# --- organizations ----------------------------------------------------------------------


# Abbreviations kept in capitals by tidy_name. Not state codes: "DE" and "LA"
# are also parts of surnames ("De La Cruz").
_KEEP_UPPER = {"LLC", "PC", "PLLC", "LLP", "LP", "DDS", "DMD", "MD", "PT", "OT", "PA",
               "II", "III", "IV", "USA", "HHC", "HHA", "ABA", "DME", "ENT", "OB", "GYN",
               "CNA", "RN", "LPN", "NP"}

_LEGAL_FORM = re.compile(
    r"[,.]?\s+\b(?:llc|l\.l\.c\.?|inc\.?|incorporated|corp\.?|corporation|co\.?|"
    r"company|ltd\.?|lp|llp|pc|p\.c\.?|pllc|pa|p\.a\.?)\s*$",
    re.IGNORECASE,
)


def tidy_name(name: str) -> str:
    """'1 UNITY HOMEHEALTH LLC' -> '1 Unity Homehealth LLC'; "DON'S" -> "Don's".
    Names already in mixed case are kept as typed."""
    name = " ".join((name or "").split())
    if not name or not (name.isupper() or name.islower()):
        return name
    words = []
    for word in name.split():
        bare = re.sub(r"[^A-Za-z]", "", word).upper()
        if bare in _KEEP_UPPER and word.upper().strip(".,") == bare:
            words.append(word.upper())
            continue
        # Letters straight after a digit stay lower case: "21st", "4th".
        word = re.sub(r"(?<![0-9a-z])[a-z]+", lambda m: m.group().capitalize(), word.lower())
        words.append(re.sub(r"'S\b", "'s", word))
    return " ".join(words)


def _key_name(name: str) -> str:
    """Comparable form of an organization name, legal form and punctuation removed."""
    name = (name or "").upper()
    for _ in range(2):
        name = _LEGAL_FORM.sub("", name)
    return " ".join(re.findall(r"[A-Z0-9]+", name))


@dataclass(frozen=True)
class Organization:
    """One healthcare organization, with its authorized official."""

    npi: str
    legal_name: str
    dba: str
    address: str
    city: str
    state: str
    zip: str
    phone: str
    specialty: str
    field: str
    enumerated: str  # YYYY-MM-DD the NPI was issued
    official_first: str
    official_last: str
    official_title: str
    official_level: str
    official_phone: str
    locations: int = 1

    @property
    def key(self) -> str:
        return self.npi

    @property
    def company_name(self) -> str:
        """The name the public knows it by: the DBA when there is one."""
        return self.dba or self.legal_name

    @property
    def search_name(self) -> str:
        """For the Google lookup: the public name, legal form removed."""
        name = self.company_name
        for _ in range(2):
            name = _LEGAL_FORM.sub("", name)
        return name.strip(" ,.")

    def years_registered(self, today: date | None = None) -> int:
        try:
            issued = datetime.strptime(self.enumerated, "%Y-%m-%d").date()
        except ValueError:
            return 0
        today = today or date.today()
        return today.year - issued.year - ((today.month, today.day) < (issued.month, issued.day))


def _location(record: dict) -> dict:
    for address in record.get("addresses") or []:
        if address.get("address_purpose") == "LOCATION":
            return address
    return {}


def _primary_specialty(record: dict) -> str:
    taxonomies = record.get("taxonomies") or []
    for taxonomy in taxonomies:
        if taxonomy.get("primary"):
            return taxonomy.get("desc") or ""
    return (taxonomies[0].get("desc") or "") if taxonomies else ""


def _dba(record: dict, legal_name: str) -> str:
    for other in record.get("other_names") or []:
        if other.get("code") == "3" or "doing business" in (other.get("type") or "").lower():
            name = (other.get("organization_name") or "").strip()
            if name and _key_name(name) != _key_name(legal_name):
                return name
    return ""


def _clean_part(value: str) -> str:
    """NPI fills empty name parts with '--'."""
    value = (value or "").strip()
    return "" if value in ("--", "-", ".") else value


def parse_record(record: dict, rules: HealthRules, searched: Field | None) -> Organization | None:
    """An Organization from one registry record, or None when it isn't a lead:
    inactive, a sub-part of a larger organization, or excluded by the rules."""
    basic = record.get("basic") or {}
    if basic.get("status", "A") != "A" or basic.get("organizational_subpart") == "YES":
        return None
    legal_name = (basic.get("organization_name") or "").strip()
    specialty = _primary_specialty(record)
    if not legal_name or rules.excluded(legal_name, specialty):
        return None

    # The primary specialty decides the field. A record found under one field
    # whose primary specialty belongs to another is that other field's lead.
    found = rules.classify(specialty)
    if found is None:
        found = searched
    if found is None:
        return None

    location = _location(record)
    title = _clean_part(basic.get("authorized_official_title_or_position"))
    first = _clean_part(basic.get("authorized_official_first_name"))
    last = _clean_part(basic.get("authorized_official_last_name"))
    return Organization(
        npi=str(record.get("number") or ""),
        legal_name=tidy_name(legal_name),
        dba=tidy_name(_dba(record, legal_name)),
        address=tidy_name(" ".join(
            p for p in (location.get("address_1"), location.get("address_2")) if p
        )),
        city=tidy_name(location.get("city") or ""),
        state=(location.get("state") or "").upper(),
        zip=(location.get("postal_code") or "")[:5],
        phone=_format_phone(re.sub(r"\D", "", location.get("telephone_number") or "")),
        specialty=specialty,
        field=found.name,
        enumerated=basic.get("enumeration_date") or "",
        official_first=tidy_name(first),
        official_last=tidy_name(last),
        official_title=title,
        official_level=rules.title_level(title) if (first or last) else "unknown",
        official_phone=_format_phone(re.sub(r"\D", "", basic.get("authorized_official_telephone_number") or "")),
    )


def group_organizations(orgs: list[Organization]) -> list[Organization]:
    """One lead per organization. The same name at several practice addresses
    is one business with several locations — the oldest registration speaks
    for it, and the location count is kept as a sign of size."""
    groups: dict[str, list[Organization]] = {}
    for org in orgs:
        groups.setdefault(_key_name(org.legal_name) or org.npi, []).append(org)

    leads = []
    for members in groups.values():
        members.sort(key=lambda o: (o.enumerated or "9999", o.npi))
        places = {(_key_name(o.address), o.zip) for o in members}
        leads.append(replace(members[0], locations=len(places)))
    return leads


# --- searching -----------------------------------------------------------------------------


class NpiClient:
    """The registry's search API, with paging, retries and a short-lived cache."""

    def __init__(self, session=None, cache_dir: Path | None = CACHE_DIR,
                 stop: threading.Event | None = None, pause: float = 0.2):
        self._session = session or requests.Session()
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.stop = stop or threading.Event()
        self.pause = pause
        self.requests = 0
        self._lock = threading.Lock()

    def _get(self, params: dict) -> dict:
        last = ""
        for attempt in range(RETRIES):
            if self.stop.is_set():
                raise SearchStopped()
            try:
                response = self._session.get(SEARCH_URL, params=params, timeout=TIMEOUT)
                with self._lock:
                    self.requests += 1
                if response.status_code == 200:
                    data = response.json()
                    errors = data.get("Errors")
                    if errors:
                        # "No taxonomy codes found" is a settings typo; say which name.
                        raise NpiUnavailable(
                            f"the NPI registry refused the search {params.get('taxonomy_description')!r}: "
                            f"{errors[0].get('description')}"
                        )
                    return data
                last = f"http {response.status_code}"
            except (requests.RequestException, ValueError) as exc:
                last = type(exc).__name__
            if self.stop.wait(2 ** attempt):
                raise SearchStopped()
        raise NpiUnavailable(f"the NPI registry didn't answer ({last}) — check the internet connection")

    def _cache_path(self, params: dict) -> Path | None:
        if self.cache_dir is None:
            return None
        digest = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:20]
        return self.cache_dir / f"{digest}.json"

    def search(self, taxonomy: str, state: str, prefix: str | None) -> tuple[list[dict], bool]:
        """Every record for one specialty in one area, and whether the
        registry's 1,200-record cap cut the list short."""
        params = {"version": "2.1", "enumeration_type": "NPI-2",
                  "taxonomy_description": taxonomy, "state": state}
        if prefix:
            params["postal_code"] = prefix if len(prefix) == 5 else prefix + "*"

        cache = self._cache_path(params)
        if cache is not None and cache.exists():
            try:
                saved = json.loads(cache.read_text(encoding="utf-8"))
                fetched = datetime.fromisoformat(saved["fetched"])
                if datetime.now() - fetched < timedelta(days=CACHE_DAYS):
                    return saved["results"], saved["capped"]
            except (ValueError, KeyError, OSError):
                pass  # an unreadable cache entry is simply fetched again

        results: list[dict] = []
        skip = 0
        while True:
            page = self._get({**params, "limit": PAGE_SIZE, "skip": skip}).get("results") or []
            results += page
            if len(page) < PAGE_SIZE or skip >= MAX_SKIP:
                break
            skip += PAGE_SIZE
            if self.pause and self.stop.wait(self.pause):
                raise SearchStopped()
        capped = len(results) >= SEARCH_CAP

        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"fetched": datetime.now().isoformat(),
                                         "results": results, "capped": capped}), encoding="utf-8")
        return results, capped

    def collect(self, taxonomy: str, state: str, prefix: str | None) -> list[dict]:
        """search(), split into narrower ZIP prefixes whenever it hits the cap."""
        results, capped = self.search(taxonomy, state, prefix)
        if not capped or (prefix and len(prefix) >= 5):
            return results
        narrower = [f"{prefix}{d}" for d in range(10)] if prefix else [f"{d:02d}" for d in range(100)]
        log.info("  %s in %s%s: over 1,200 results, searching ZIP by ZIP",
                 taxonomy, state, f" {prefix}*" if prefix else "")
        merged: list[dict] = []
        for sub in narrower:
            merged += self.collect(taxonomy, state, sub)
        return merged

    def close(self) -> None:
        self._session.close()


def find_organizations(
    client: NpiClient,
    rules: HealthRules,
    state: str,
    zip_prefixes: tuple[str, ...],
    field_names: tuple[str, ...] | None = None,
) -> list[Organization]:
    """Every lead organization in the area for the chosen fields (all when None)."""
    fields = [rules.field_named(n) for n in field_names] if field_names else list(rules.fields)
    wanted = {f.name for f in fields}
    jobs = [(f, t, p) for f in fields for t in f.taxonomies for p in (zip_prefixes or (None,))]

    def run(job):
        f, taxonomy, prefix = job
        return f, client.collect(taxonomy, state, prefix)

    records: dict[str, tuple[dict, Field]] = {}
    with ThreadPoolExecutor(max_workers=SEARCH_THREADS) as pool:
        for f, found in pool.map(run, jobs):
            for record in found:
                records.setdefault(str(record.get("number")), (record, f))
    log.info("NPI registry: %d organizations found (%d searches)", len(records), client.requests)

    orgs = []
    for record, searched in records.values():
        org = parse_record(record, rules, searched)
        if org is None or org.field not in wanted or org.state != state:
            continue
        if zip_prefixes and not org.zip.startswith(zip_prefixes):
            continue  # matched on its mailing address, practice is elsewhere
        orgs.append(org)
    return group_organizations(orgs)
