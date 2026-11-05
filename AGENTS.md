# AGENTS.md

Guide for people joining this repository, and for AI coding agents working in it.

Gonk produces a daily email of objects entering and leaving Earth orbit, built from
the Space-Track.org satellite catalog. See `README.md` for what the report contains.

## Joining the project

The repository owner invites you on GitHub (*Settings → Collaborators*). Then:

* Use your **own** Space-Track account and run everything on your own machine.
  Your login, snapshot, query record and CSV data stay in your clone (`.env`,
  `state/`, `reports/`, `data/`), which are git-ignored. Nothing personal is
  shared through the repository.
* Your install tracks its own API usage, so your runs don't affect anyone else's
  account limits, and theirs don't affect yours.
* Changing the code: work on a branch, run `python -m unittest -v`, and open a pull
  request. Don't commit anything from `state/`, `reports/`, `data/` or `.env`.

## Get started (people)

You need Python 3.9+ and your **own** free Space-Track account
(https://www.space-track.org/auth/createAccount). Space-Track accounts are personal;
don't share one between people.

```bash
git clone https://github.com/JackBeerman/Gonk.git
cd Gonk
pip install .            # installs the `gonk` command
gonk setup               # asks for your Space-Track username and password, checks the login,
                         # optionally asks for email (SMTP) settings, and saves them to .env
gonk rules               # shows which Space-Track API calls are allowed right now
gonk run                 # after 17:00 UTC: first run takes the baseline, later runs send the daily report
```

Each run writes the email report to `reports/` and updates a CSV dataset in `data/`:
the baseline, one file of new objects and one of removed objects per day, and running
totals (`new_all.csv`, `removed_all.csv`). See "CSV dataset" in `README.md`.

`gonk setup` stores your settings in `.env` in the repository folder. The file is
readable only by you (mode 600) and is git-ignored. Run `gonk setup` again at any
time to change a value; press Enter to keep the current one.

To run it every day, see "Run it every day" in `README.md` (cron, or GitHub Actions
with the workflow in `deploy/github-actions/`).

## Credentials

* Enter credentials only through `gonk setup`, a local `.env`, or GitHub Actions
  repository secrets.
* Never commit `.env`, and never put a password in code, issues, pull requests,
  commit messages, logs or chat messages.
* For GitHub Actions, add `SPACETRACK_USER` and `SPACETRACK_PASS` (and the SMTP
  settings) under *Settings → Secrets and variables → Actions*.

## Space-Track API rules

Space-Track suspends accounts that break its API use guidelines. Gonk enforces them in
`gonk/spacetrack.py`, and `gonk rules` prints them, with the last and next allowed
time for each call:

| Class | Limit | Used for |
|---|---|---|
| `satcat` | 1 per UTC day, after 17:00 UTC | Full on-orbit catalog: baseline, then a weekly re-sync |
| `satcat_debut` | 1 per UTC day, after 17:00 UTC | Objects added to the catalog since the last run |
| `satcat_change` | 1 per UTC day, after 17:00 UTC | Re-entries, renames, country changes |
| `decay` | 1 per UTC day, after 17:00 UTC | Re-entry epochs and forecasts |
| `boxscore` | 1 per UTC day, after 17:00 UTC | Country names (cached weekly) |
| `gp` | 1 per hour | Orbital elements and TLEs for new objects |
| `launch_site` | 1 per 24 hours (self-imposed) | Launch site names (cached weekly) |

Account-wide: fewer than 30 requests per minute and 300 per hour (Gonk uses at most
20 and 200, 2.5 s apart). A daily run makes about 6–8 requests.

Things to know:

* **One deployment per account.** The record of past queries (`state/ledger.json`)
  is local to each install. If you run Gonk both on your laptop and in GitHub
  Actions with the same account, neither knows about the other's queries.
* **Don't use `--force`** unless you are sure the account has not queried that
  class today. It skips the per-class checks.
* Queries you make outside Gonk (the Space-Track website's query builder, other
  scripts) count against the same account limits.

## For AI coding agents

* **Do not call the live Space-Track API while developing or testing.** Use the
  offline tests (`python -m unittest -v`); `tests/test_gonk.py` has a
  `FakeSpaceTrack` stand-in that returns canned rows. A live run is for the human to
  start, after 17:00 UTC.
* **Never ask a person to paste a password into chat.** Ask them to run
  `gonk setup` in their own terminal, or to add repository secrets.
* Never print, log or commit credentials, `.env`, or the `state/`, `reports/` and
  `data/` folders (they hold downloaded Space-Track data).
* Keep CSV columns fixed (`report.CSV_COLUMNS`); add new columns at the end so older
  daily files still line up.
* All Space-Track access goes through `SpaceTrack.fetch()`. To use a new API class,
  first add a `Rule` for it in `RULES` (`gonk/spacetrack.py`), with the limit from
  https://www.space-track.org/documentation#/api. Unlisted classes are refused.
* Never loop over satellites with one request each; pass NORAD ids together with
  `fetch(..., ids=[...])`, which batches them into comma-delimited queries.
* Every fact shown in the report needs a source and a confidence rating
  (`gonk/confidence.py`). If you add a data source or field, add its rating rule
  there and to the `METHOD` table so readers can see how it is rated.
* Keep the HTML email under about 95 KB (Gmail clips larger messages). `report.py`
  shrinks the tables automatically; check `len(html)` if you add sections.

## Repository layout

| Path | What it is |
|---|---|
| `gonk/spacetrack.py` | API client, rate limiter, per-class rules and query ledger |
| `gonk/pipeline.py` | Baseline and daily diff (new, removed, renamed, forecasts) |
| `gonk/catalog.py` | Record normalization, orbit classification, constellation names |
| `gonk/confidence.py` | Source and confidence rating rules |
| `gonk/satnogs.py` | Optional radio-frequency lookup (SatNOGS DB) |
| `gonk/report.py` | Email HTML, plain text and CSV attachments |
| `gonk/export.py` | CSV dataset in `data/`: baseline, daily files, running totals |
| `gonk/emailer.py` | SMTP sending |
| `gonk/cli.py` | `gonk` command: setup, rules, run, baseline, daily, status |
| `deploy/github-actions/` | Workflow to copy into `.github/workflows/` |
| `tests/` | Offline tests; no network or account needed |

## Checks before you push

```bash
python -m unittest -v
```
