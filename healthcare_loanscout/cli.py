"""Command-line entry point and run orchestration.

    run  search the NPI registry for healthcare organizations in an area (free),
         rank them, look the best up on Google via Serper (1 credit each),
         crawl their websites, score, and append to the CSV

A run takes an optional threading.Event so the window's Stop button can end
it the same way Ctrl+C does: finished work is kept, the rest resumes on the
next run.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

from .config import ConfigError, load_settings
from .crawl import DEFAULT_MAX_PAGES
from .emailcheck import EmailChecker
from .export import CsvWriter, build_row, sort_csv_by_score
from .fetch import PoliteFetcher
from .leads import build_lead
from .npi import (NpiClient, NpiUnavailable, SearchStopped, check_zip_prefixes,
                  find_organizations, load_health_rules)
from .places import PlacesClient, SerperUnavailable

log = logging.getLogger("healthcare_loanscout")

EXIT_OK = 0
EXIT_NO_MATCHES = 1  # the registry has no lead organizations for this area and field
EXIT_CONFIG_ERROR = 2  # missing key, bad settings, registry unreachable
EXIT_DEPLETED = 3  # every matching organization already processed
EXIT_LOOKUPS_FAILED = 4  # organizations were attempted and none could be looked up
EXIT_SERPER_UNAVAILABLE = 5  # bad key or out of credits; the run stopped early
EXIT_INTERRUPTED = 130

DEFAULT_OUT = "out/healthcare_leads.csv"


@dataclass
class RunOptions:
    """What a run should do. None means 'use the healthcare.yaml setting'."""

    state: str | None = None
    zip_prefixes: tuple[str, ...] | None = None
    fields: tuple[str, ...] | None = None  # None = every field
    limit: int | None = None
    dry_run: bool = False
    out: str = DEFAULT_OUT
    max_pages: int = 8
    workers: int = 8
    delay: float = 1.0
    no_resume: bool = False


@dataclass
class RunSummary:
    """Where every organization ended up. Printed at the end of a run."""

    matching: int = 0
    already_done: int = 0
    attempted: int = 0
    written: int = 0
    no_email: int = 0
    shared_email: int = 0
    retry_later: int = 0
    with_name: int = 0
    with_owner: int = 0
    with_phone: int = 0
    with_email: int = 0
    with_website: int = 0
    not_found: int = 0
    credits_used: int = 0
    serper_unavailable: bool = False

    def outcome(self) -> int:
        if self.serper_unavailable:
            return EXIT_SERPER_UNAVAILABLE
        if self.matching == 0:
            return EXIT_NO_MATCHES
        if self.attempted == 0:
            return EXIT_DEPLETED
        if self.written == 0 and self.retry_later:
            return EXIT_LOOKUPS_FAILED
        return EXIT_OK

    def report(self, out_path: str) -> None:
        line = "-" * 58
        log.info(line)
        log.info("%-32s %d", "organizations matching", self.matching)
        if self.already_done:
            log.info("%-32s %d", "skipped (already done)", self.already_done)
        log.info("%-32s %d", "processed this run", self.attempted)
        if self.retry_later:
            log.info("%-32s %d", "  failed (will retry)", self.retry_later)
        if self.no_email:
            log.info("%-32s %d", "  left out: no working email", self.no_email)
        if self.shared_email:
            log.info("%-32s %d", "  left out: email already used", self.shared_email)
        log.info("%-32s %d", "  with a first and last name", self.with_name)
        log.info("%-32s %d", "    who is the owner/president", self.with_owner)
        log.info("%-32s %d", "  with a phone number", self.with_phone)
        log.info("%-32s %d", "  with an email", self.with_email)
        log.info("%-32s %d", "  with a website", self.with_website)
        if self.not_found:
            log.info("%-32s %d", "  no Google listing (closed?)", self.not_found)
        log.info("%-32s %d", "Serper credits used", self.credits_used)
        log.info("%-32s %d  ->  %s", "rows written", self.written, out_path)
        log.info(line)


def preview_path(out: str) -> str:
    return out[:-4] + "_preview.csv" if out.endswith(".csv") else out + "_preview.csv"


def run_leads(options: RunOptions, stop: threading.Event | None = None,
              npi_client: NpiClient | None = None, places=None) -> int:
    """npi_client and places can be passed in (tests); otherwise real ones are made."""
    stop = stop or threading.Event()
    try:
        settings = load_settings()
        health = load_health_rules()
        state = (options.state or health.state).upper()
        prefixes = health.zip_prefixes if options.zip_prefixes is None else options.zip_prefixes
        check_zip_prefixes(prefixes)
        fields = options.fields
        for name in fields or ():
            health.field_named(name)  # an unknown name stops here, with the list
        api_key = None if options.dry_run or places is not None else settings.require_serper_key()
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG_ERROR

    rules = settings.rules
    limit = health.default_limit if options.limit is None else options.limit
    area = f"{state} ZIP {', '.join(p + '*' for p in prefixes)}" if prefixes else f"all of {state}"
    log.info("searching the NPI registry: %s, %s (free)...",
             ", ".join(fields) if fields else "every field", area)

    client = npi_client or NpiClient(stop=stop)
    try:
        orgs = find_organizations(client, health, state, prefixes, fields)
    except SearchStopped:
        log.warning("stopped during the NPI search — nothing was written")
        return EXIT_INTERRUPTED
    except NpiUnavailable as exc:
        log.error("%s", exc)
        return EXIT_CONFIG_ERROR
    finally:
        if npi_client is None:
            client.close()

    summary = RunSummary(matching=len(orgs))
    log.info("%d organizations are leads after removing hospitals, public bodies and sub-units", len(orgs))
    if not orgs:
        return EXIT_NO_MATCHES

    # Rank on NPI facts alone (free), so credits go to the strongest first.
    ranked = sorted(
        orgs,
        key=lambda o: (build_lead(o, None, None, rules, health, options.max_pages).score.total,
                       o.years_registered(), o.company_name),
        reverse=True,
    )

    if options.dry_run:
        return _preview(ranked, options, limit, rules, health, summary)

    places = places or PlacesClient(api_key)
    fetcher = PoliteFetcher(settings.user_agent, default_delay=options.delay)
    checker = EmailChecker()
    used_emails = set() if options.no_resume else emails_in(options.out)

    try:
        with CsvWriter(options.out, resume=not options.no_resume) as writer:
            pending = [o for o in ranked if not writer.already_done(o.key)]
            summary.already_done = len(ranked) - len(pending)
            if limit > 0:
                pending = pending[:limit]

            if not pending:
                log.info("nothing new — every matching organization is already done")
                summary.report(options.out)
                return EXIT_DEPLETED

            log.info("looking up the top %d on Google (about %d Serper credits)",
                     len(pending), len(pending))

            with ThreadPoolExecutor(max_workers=options.workers) as pool:
                futures = {
                    pool.submit(build_lead, o, places, fetcher, rules, health, options.max_pages,
                                email_checker=checker): o
                    for o in pending
                }
                not_done = set(futures)
                try:
                    while not_done:
                        if stop.is_set():
                            raise KeyboardInterrupt
                        # Polled once a second so Stop and Ctrl+C are noticed promptly.
                        done, not_done = wait(not_done, timeout=1.0, return_when=FIRST_COMPLETED)
                        for future in done:
                            org = futures[future]
                            summary.attempted += 1
                            try:
                                lead = future.result()
                            except SerperUnavailable as exc:
                                log.error("%s — stopping; finished rows are saved", exc)
                                summary.serper_unavailable = True
                                raise
                            except Exception as exc:  # one bad organization must not kill the run
                                summary.retry_later += 1
                                log.warning("%s: failed, will retry next run (%s)", org.company_name, exc)
                                continue

                            if health.require_email and (not lead.email or lead.email in used_emails):
                                # Done either way: the lookup was paid for, and
                                # a re-run would find the same website.
                                writer.mark_done(org.key)
                                if lead.email:
                                    summary.shared_email += 1
                                    why = f"{lead.email} already used for another lead"
                                else:
                                    summary.no_email += 1
                                    why = "no working email found"
                                log.info("%-36.36s left out: %s", org.company_name, why)
                                continue

                            row = build_row(lead, rules)
                            writer.write(row)
                            used_emails.add(lead.email)
                            _tally(summary, row, lead)
                            log.info("%-36.36s %-22.22s score=%-5s %s",
                                     org.company_name,
                                     f"{row['first_name']} {row['last_name']}".strip() or "-",
                                     row["score"], row["email"] or row["phone"] or "-")
                except (KeyboardInterrupt, SerperUnavailable):
                    fetcher.stop()
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
    except KeyboardInterrupt:
        log.warning("stopped — %d rows already written to %s (run again to continue)",
                    summary.written, options.out)
        return EXIT_INTERRUPTED
    except SerperUnavailable:
        pass
    finally:
        summary.credits_used = getattr(places, "lookups", 0)
        fetcher.close()
        places.close()

    sort_csv_by_score(options.out)
    summary.report(options.out)
    return summary.outcome()


def emails_in(path: str) -> set[str]:
    """Emails already in the output, so a later run doesn't list an inbox twice."""
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            return {row["email"] for row in csv.DictReader(handle) if row.get("email")}
    except (OSError, KeyError):
        return set()


def _tally(summary: RunSummary, row: dict, lead) -> None:
    summary.written += 1
    if row["first_name"] and row["last_name"]:
        summary.with_name += 1
        if lead.org.official_level == "owner":
            summary.with_owner += 1
    if row["phone"]:
        summary.with_phone += 1
    if row["email"]:
        summary.with_email += 1
    if row["website"]:
        summary.with_website += 1
    if "may have closed" in lead.listing.note:
        summary.not_found += 1


def _preview(ranked, options, limit, rules, health, summary) -> int:
    """Score from NPI facts alone, into a separate file. Nothing is marked
    done, so a real run afterwards still processes every organization."""
    out = preview_path(options.out)
    with CsvWriter(out, resume=False, track_seen=False) as writer:
        for org in ranked:
            lead = build_lead(org, None, None, rules, health, options.max_pages)
            row = build_row(lead, rules)
            writer.write(row)
            summary.attempted += 1
            _tally(summary, row, lead)

    sort_csv_by_score(out)
    next_run = min(limit, len(ranked)) if limit > 0 else len(ranked)
    log.info("preview only — no Google lookups made. The next real run would look up the "
             "top %d (about %d credits).", next_run, next_run)
    if health.require_email:
        log.info("the registry has no emails: a Run finds them on each website, and leaves out "
                 "every organization without a working one")
    summary.report(out)
    return EXIT_OK


# --- command line ----------------------------------------------------------------------


def _zip_list(text: str) -> tuple[str, ...]:
    """'191,190' -> ('191', '190'); 'all' -> () meaning the whole state."""
    if text.strip().lower() in ("", "all"):
        return ()
    return tuple(z.strip() for z in text.split(",") if z.strip())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="healthcare_loanscout",
        description="Find healthcare organizations from the NPI registry, with the owner's "
        "name, phone, email and an evidence-backed score.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="find, look up and score leads")
    run.add_argument("--state", help="two-letter state code (default: healthcare.yaml, PA)")
    run.add_argument("--zip", type=_zip_list,
                     help="ZIP code starts, 2-5 digits, comma-separated, e.g. 191 or 191,190; "
                     "'all' for the whole state")
    run.add_argument("--field", action="append", metavar="NAME",
                     help="only this field, as named in healthcare.yaml (repeat for several), "
                     "e.g. --field 'Home care'; default: all")
    run.add_argument("--limit", type=int,
                     help="organizations looked up this run (default: healthcare.yaml, 50; 0 = no cap)")
    run.add_argument("--dry-run", action="store_true",
                     help="free preview from the NPI registry only; no lookups, no credits")
    run.add_argument("--out", default=DEFAULT_OUT, help="output CSV path")
    run.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="website pages per organization")
    run.add_argument("--workers", type=int, default=8, help="organizations processed at once")
    run.add_argument("--delay", type=float, default=1.0, help="seconds between requests to one website")
    run.add_argument("--no-resume", action="store_true",
                     help="start the output over instead of skipping organizations already done")
    run.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return run_leads(
        RunOptions(
            state=args.state,
            zip_prefixes=args.zip,
            fields=tuple(args.field) if args.field else None,
            limit=args.limit,
            dry_run=args.dry_run,
            out=args.out,
            max_pages=args.max_pages,
            workers=args.workers,
            delay=args.delay,
            no_resume=args.no_resume,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
