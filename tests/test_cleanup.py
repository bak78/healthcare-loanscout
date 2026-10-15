"""Keeping results to small-business leads: chains and health systems move to
the left-out file, disqualified organizations never reach the results, and no
paid email search is spent on them."""

import csv

from healthcare_loanscout import cli, leads
from healthcare_loanscout.cleanup import leave_out_chains, left_out_path, never_a_chain
from healthcare_loanscout.emailfinder import FinderResult
from healthcare_loanscout.export import COLUMNS
from test_run import TODAY, StubPlaces, _client, _rows, crawl_of, health, org, workdir  # noqa: F401

NEVER = never_a_chain(["visitingangels.com"], ["facebook.com"])


def results(tmp_path, *rows):
    path = tmp_path / "leads.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for i, (name, email, website) in enumerate(rows):
            writer.writerow({"key": str(i), "company_name": name, "email": email, "website": website})
    return path


def names(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return [r["company_name"] for r in csv.DictReader(handle)]


def test_three_practices_on_one_health_systems_domain_move_out(tmp_path):
    path = results(tmp_path,
                   ("Garcia Podiatry", "jgarcia@christianacare.org", ""),
                   ("Ccvh Primary Care", "info@christianacare.org", "https://christianacare.org/ccvh"),
                   ("Christiana Visiting Nurses", "", "https://www.christianacare.org/vna"),
                   ("Unity Home Health", "jobrien@unityhh.com", "https://unityhh.com/"))
    moved = leave_out_chains(path, COLUMNS, 3, NEVER)
    assert moved == {"christianacare.org": 3}
    assert names(path) == ["Unity Home Health"]
    with left_out_path(path).open(newline="", encoding="utf-8") as handle:
        out = list(csv.DictReader(handle))
    assert len(out) == 3 and out[0]["left_out_reason"] == "chain or group: christianacare.org is shared by 3 companies"


def test_two_companies_sharing_a_domain_stay(tmp_path):
    path = results(tmp_path, ("A", "a@group.com", ""), ("B", "b@group.com", ""))
    assert leave_out_chains(path, COLUMNS, 3, NEVER) == {}
    assert names(path) == ["A", "B"]


def test_email_providers_website_builders_and_franchises_are_never_chains(tmp_path):
    rows = [(f"Agency {i}", f"owner{i}@{d}", "") for i, d in enumerate(["gmail.com", "cox.net", "comcast.net"] * 3)]
    rows += [(f"Wix {i}", "", f"https://agency{i}.wixsite.com/home") for i in range(3)]
    rows += [(f"Visiting Angels {i}", f"o{i}@visitingangels.com", "https://www.visitingangels.com/x") for i in range(3)]
    path = results(tmp_path, *rows)
    assert leave_out_chains(path, COLUMNS, 3, NEVER) == {}
    assert len(names(path)) == len(rows)


def test_zero_turns_it_off(tmp_path):
    path = results(tmp_path, *[(f"P{i}", f"x{i}@system.org", "") for i in range(5)])
    assert leave_out_chains(path, COLUMNS, 0, NEVER) == {}


# --- disqualified organizations -----------------------------------------------------------------

NONPROFIT = "<p>We are a 501(c)(3) nonprofit organization serving our community.</p>"


class CountingFinder:
    name = "Anymailfinder"
    lookups = found = 0

    def __init__(self):
        self.asked = []

    def find(self, *args, **kwargs):
        self.asked.append(args)
        return FinderResult()

    def close(self):
        pass


def test_no_paid_search_for_an_organization_its_website_disqualifies(rules, health, monkeypatch):
    finder = CountingFinder()
    monkeypatch.setattr(leads, "crawl_site", lambda *a, **k: crawl_of(("https://unityhh.com/", NONPROFIT)))
    lead = leads.build_lead(org(health), StubPlaces(), object(), rules, health, 8, TODAY, email_finder=finder)
    assert lead.score.disqualified
    assert finder.asked == [], "no credit spent on a nonprofit"


def test_a_disqualified_organization_goes_to_the_left_out_file(workdir, monkeypatch):
    monkeypatch.setattr(leads, "crawl_site", lambda *a, **k: crawl_of(("https://unityhh.com/", NONPROFIT)))
    options = cli.RunOptions(fields=("Home care",), zip_prefixes=("191",), limit=0, out="out/leads.csv", workers=1)
    assert cli.run_leads(options, npi_client=_client(2), places=StubPlaces()) == cli.EXIT_OK
    assert _rows(workdir / "out/leads.csv") == []
    left = _rows(workdir / "out/leads_left_out.csv")
    assert len(left) == 2 and left[0]["left_out_reason"].startswith("disqualified: nonprofit")
    # Done, so a re-run doesn't pay to look them up again.
    assert cli.run_leads(options, npi_client=_client(2), places=StubPlaces()) == cli.EXIT_DEPLETED
