# Healthcare LoanScout

> **Prototype, branded edition.** Capital Sense Funding's colours and logo are
> in the window. The logo in `assets\` is the company's — keep this project
> private and check before sharing it outside the company.

Finds **healthcare organizations** — home care agencies, dental practices,
therapy clinics and more — in the federal NPI registry, with the **owner's
first and last name**. It then looks each one up on Google to get a current
phone number, rating and website, reads the website for an email address and
signs of growth, and writes a ranked CSV of leads with the evidence behind
every score.

```
NPI registry ──▶ owner name, ──▶ Google listing ──▶ website crawl ──▶ healthcare_leads.csv
(official, free,  field, rank     (via Serper:       (email, growth
 nationwide)                       phone, rating)     signals)
```

Every healthcare provider that bills insurance must register in the NPI
registry, and each organization's record names its **authorized official** —
usually the owner or president — with a title and a direct phone number. So
the registry gives both the lead list and the person to call, in every state,
for free.

## The easy way: the window

Double-click **`Healthcare LoanScout GUI.bat`**. The first time, it sets
itself up (about a minute). Then:

1. **Where** — pick a state and type the start of the ZIP codes (`191` is
   Philadelphia; at least two digits; blank = the whole state). Every
   healthcare field is searched; the spreadsheet's `healthcare_industry`
   column says which each organization is in, so filter there.
2. **Preview (free)** — searches the registry and ranks every matching
   organization from registry facts alone. Writes
   `out\healthcare_leads_preview.csv`. No credits used.
3. **Run** — looks up the top organizations on Google (about 1 Serper credit
   each), crawls their websites, and writes `out\healthcare_leads.csv`. Run
   again to continue down the list; organizations already done are skipped.
4. **Open results** opens the newest spreadsheet. **Stop** ends a run early;
   finished rows are kept.

*Run* needs a Serper key in `.env` (below). *Preview* doesn't.

## What's in the spreadsheet

The first columns are the ones to work from:

| Column | Meaning |
|---|---|
| `first_name`, `last_name`, `title` | The authorized official from the NPI registry |
| `decision_maker` | owner / president · clinician (likely owner) · manager / administrator · office staff |
| `email` | From the organization's website. The official's own address (jane.doe@…) is preferred over info@ |
| `phone` | Google listing's phone, else the NPI practice phone, else the website's |
| `company_name` | The name the public knows it by (its DBA when it has one) |
| `healthcare_industry` | e.g. *Home care*, *Dental*, *Behavioral health* — one of the fields below |
| `specialty` | the exact NPI specialty, e.g. *In Home Supportive Care* |
| `score` | 0–100 weight; see below |
| `google_rating`, `google_reviews` | From the Google listing found through Serper |
| `official_phone` | The official's own number from the registry — often a direct line |

Then the website, legal name, address, NPI number, years registered,
locations, the top three pieces of evidence, and notes.

## The score (weight)

The same engine as PPP LoanScout: every point comes from a piece of evidence,
shown in the `evidence_` columns. Three kinds of evidence:

- **NPI registry** (free): in a lead field; registered 3+ years; the official
  is the owner (14), a clinician (10), a manager (5); 2+ practice locations.
- **Google, via Serper** (the Run step): a listing matched at the registry
  address (10); 50+ ratings (14) or 10–49 (7); 4.0 stars or better (5). A busy
  listing is the best public sign of a practice's size.
- **Website**: an email found (6), the official's own email (4), and growth
  signals — hiring caregivers or nurses, accepting new patients, new
  locations, Medicare/Medicaid certification. Lenders, public companies and
  similar are disqualified.

All weights are in `config\healthcare.yaml` (registry and Google) and
`config\signals.yaml` (website). Edit and re-run.

A preview scores registry facts only, so its scores are lower; the Run adds
Google and the website on top.

## Only leads with a working email

Every row in `healthcare_leads.csv` has an email that passed these checks (set
`require_email: false` in `config\healthcare.yaml` to turn this off):

- it is on the business's own website and domain, or a gmail/yahoo-style
  mailbox listed there — not a web designer's or a directory's;
- it is a real address — not a placeholder (none@, test@) or no-reply box,
  and not a misspelled domain (gmial.com);
- its domain exists and accepts mail (DNS lists a mail server for it);
- no other row already uses it.

The registry has no emails, so this happens after the Google lookup: an
organization whose website gives no working email still costs its credit, is
left out, and isn't looked up again. The preview can't check emails yet, so
it lists every organization.

These checks can't tell whether one mailbox still exists (whether jsmith@ left
the company). A paid verifier — NeverBounce, ZeroBounce — checks that for
about a cent an address; worth it before a large send.

## Email finder: Anymailfinder or Hunter.io (optional)

With `ANYMAILFINDER_API_KEY` (or `HUNTER_API_KEY`) in `.env`, a Run asks the
email finder for the official's own email whenever the website shows none: it
takes the official's name (from the registry) and the website's domain (from
Google) and returns the likely address. Only **verified** addresses are kept;
"risky", "accept all" and "unknown" ones are logged and left out. The CSV's
`email_source` column says "Anymailfinder (verified)" for these. When both
keys are set, Anymailfinder is used.

Anymailfinder charges a credit only for each verified email it finds; a miss
is free, and so is repeating a search within 30 days. (Hunter charges for each
address it returns, verified or not.) Set `email_finder_when: always` in
`config\healthcare.yaml` to also replace front-desk addresses (info@) with
the official's own — more credits, better contacts.

An email finder needs a website. An organization with no Google listing, or a
listing without a website, can't get an email from any such service.

## Fields

`config\healthcare.yaml` lists the fields and the NPI specialty names each
one covers — home care, hospice, nursing & assisted living, adult day care,
behavioral health, dental, therapy, chiropractic, eye care, foot care,
primary & urgent care, pharmacy, medical equipment, medical transport, labs,
surgery centers. Hospitals, government bodies, universities and sub-units of
larger organizations are left out.

## Who else is left out

- **Disqualified organizations** — nonprofits, lenders, public and very large
  companies, found on their websites during a Run. They go to
  `out\healthcare_leads_left_out.csv` with the reason, not the results, and
  no email-finder credit is spent on them.
- **Health systems and chains** — each practice of a health system registers
  its own NPI, so the registry can't tell "Garcia Podiatry" belongs to
  ChristianaCare. At the end of every Run, a website or email domain shared by
  3 or more different organizations in the results moves them to the
  left-out file. Email providers, website builders, government sites and the
  franchise brands in `franchise_domains` in `config\healthcare.yaml` never
  count — a Visiting Angels office is an independently owned small business.
  `chain_min_companies: 0` turns this off.

Anything in the left-out file can be copied back into the results by hand.

## Command line

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
notepad .env

.venv\Scripts\python -m healthcare_loanscout run --dry-run                       # free preview
.venv\Scripts\python -m healthcare_loanscout run --field "Home care" --limit 50  # ~50 credits
.venv\Scripts\python -m healthcare_loanscout run --state NJ --zip 080,081 --field Dental
```

| `run` option | Default | Meaning |
|---|---|---|
| `--state` | `PA` | two-letter state |
| `--zip` | `191` | ZIP code starts (2–5 digits), comma-separated; `all` for the whole state |
| `--field` | all | one field as named in healthcare.yaml; repeat for several |
| `--limit` | `50` | organizations looked up this run; `0` = no cap |
| `--dry-run` | off | free preview, no Google lookups |
| `--no-resume` | off | start the CSV over |

Registry searches are cached in `data\npi_cache` for 7 days.

## What it can't do yet

- **No Google listing ≠ closed.** Many home care agencies are run from a
  home office and have no Google listing; such rows say "may have closed or
  moved" and get no Google points, but the name and phones still come from
  the registry. In a first test, 8 of 15 Philadelphia home care agencies and
  9 of 10 dental practices had a matching listing.
- **Emails come only from websites**, so about half the home care agencies
  have none and are left out. Some site emails are not ideal (a recruiting
  address, the front desk); check before mailing.
- The authorized official is whoever signed the registration. Usually the
  owner, but check the `title` column.

## Tests

```powershell
.venv\Scripts\python -m pytest
```
