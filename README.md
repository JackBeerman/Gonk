# Gonk

A daily email report of what entered and left Earth orbit, built from the
[Space-Track.org](https://www.space-track.org) public satellite catalog.

* **First run** takes a baseline: every tracked object currently in orbit (about 35,000).
* **Every day after that** it reports:
  * **New in orbit**: objects added to the catalog since the last run, grouped by
    launch, with country, launch site, object type, orbit (regime, sun-synchronous
    or not, perigee/apogee, inclination, period, revs/day), radar size, current
    orbital elements and TLE, and radio frequencies where SatNOGS lists them.
  * **No longer in orbit**: objects that re-entered (with re-entry time and days
    in orbit) or left the public catalog.
  * **Renamed / identified**: placeholder names like `OBJECT A` replaced by real names.
  * **Predicted re-entries** in the next 7 days.
  * Charts: new objects by country, type, orbit and constellation; a 30-day
    trend of new vs. removed; and the composition of everything in orbit.
* The report is a self-contained HTML email (renders in Gmail, Outlook and Apple
  Mail) with CSV attachments that include every object and every field.

Anyone with a free Space-Track account can run it.

## Quick start

```bash
git clone https://github.com/JackBeerman/Gonk.git && cd Gonk
pip install .               # installs the `gonk` command
gonk setup                  # enter your Space-Track login (checked), optional email settings
gonk rules                  # see which API calls are allowed right now
gonk run                    # first time: baseline. After that: daily report
```

New to the repository? Read [`AGENTS.md`](AGENTS.md) first. It covers setup,
credentials and the API rules, for people and for AI coding agents.

Reports are written to `reports/<date>-<kind>/` (`report.html`, CSVs,
`summary.txt`), and `reports/latest.html` always holds the most recent one. If
email is configured, the report is also sent.

> **Run it after 17:00 UTC.** Space-Track publishes catalog updates around then
> and asks automated scripts to fetch catalog data at most once per day, after
> 17:00 UTC. Gonk refuses to query earlier (see *API rules* below).

## CSV dataset

Alongside the email, every run updates a CSV dataset in `data/` (change it with
`--data-dir` or `GONK_DATA_DIR`):

| File | Contents |
|---|---|
| `baseline_YYYY-MM-DD.csv` | Every object in orbit when the baseline was taken |
| `new_YYYY-MM-DD.csv` | Objects added to the catalog in that day's run |
| `removed_YYYY-MM-DD.csv` | Objects that left orbit in that day's run |
| `new_all.csv` | Every new object from every run, with a `run_date` column |
| `removed_all.csv` | Every removed object from every run, with a `run_date` column |

A daily file is written even when nothing changed (header only), so there are no
gaps. All files have the same columns, including each field's source and confidence
rating, so you can stack them or open them together. On GitHub Actions, `data/` is
carried between runs in the cache and attached to each run's artifact.

## Run it every day

**Option A: GitHub Actions (no server needed).** The workflow in
`deploy/github-actions/daily-orbit-report.yml` runs at 17:41 UTC daily.

1. Fork or copy this repository, then copy that file to
   `.github/workflows/daily-orbit-report.yml` (on GitHub: *Add file → Create new
   file*, paste the contents, commit).
2. In the repository settings, go to *Secrets and variables → Actions* and add:
   `SPACETRACK_USER`, `SPACETRACK_PASS`, and for email `SMTP_HOST`, `SMTP_PORT`,
   `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM`, `EMAIL_TO`.
3. Enable Actions on the fork. To start immediately (after 17:00 UTC), use
   *Actions → Daily orbit report → Run workflow*.

State (the snapshot and query ledger) is kept in the Actions cache between runs.
If the cache is lost, the next run takes a fresh baseline. Each report is also
uploaded as a workflow artifact for 30 days. GitHub pauses scheduled workflows in
repositories with no activity for 60 days, so re-enable it if the emails stop.

**Option B: cron on your own machine or server.**

```cron
41 17 * * *  cd /path/to/Gonk && /usr/bin/python3 -m gonk run >> gonk.log 2>&1
```

## Email setup

Any SMTP server works. For Gmail, use `smtp.gmail.com`, port `587`, and an
[App Password](https://myaccount.google.com/apppasswords) (it requires
2-step verification). `EMAIL_TO` takes a comma-separated list. Leave email unset
to only write the HTML report to disk; you can also run with `--no-email`.

## Commands

| Command | What it does |
|---|---|
| `gonk setup` | Enter your Space-Track login (verified) and optional email settings; saved to a private `.env`. |
| `gonk rules` | Show every Space-Track API call Gonk can make, its limit, and when it is next allowed. |
| `gonk run` | Baseline on first use, daily report afterwards. Use this one for scheduling. |
| `gonk baseline [--reset] [--from-json FILE]` | Take or retake the baseline; `--from-json` imports a SATCAT JSON export. |
| `gonk daily [--full]` | Daily report; `--full` also re-downloads and diffs the full catalog. |
| `gonk status` | Show the snapshot and when each API class was last queried. |

Global options: `--state-dir` (default `./state`), `--out-dir` (default
`./reports`), `--no-email`, `--force`, `-v`.

## API rules (and how Gonk follows them)

Space-Track can suspend accounts that break its
[API use guidelines](https://www.space-track.org/documentation#/api). Gonk
enforces them in code (`gonk/spacetrack.py`):

| Guideline | What Gonk does |
|---|---|
| Fewer than 30 requests/min and 300/hour | At most 20/min and 200/hour, with at least 2.5 s between requests. A normal daily run makes about 6–8 requests. |
| SATCAT, SATCAT_DEBUT, DECAY, BOXSCORE: once per day, after 17:00 UTC | A persistent ledger (`state/ledger.json`) refuses a second query of a class on the same UTC day, and any query before 17:00 UTC. |
| GP (orbital elements): at most once per hour | Ledger-enforced; fetched only for that day's new objects. |
| Don't re-download the catalog | Daily runs fetch only changes (`satcat_debut`, `satcat_change`, `decay`). The full on-orbit catalog is re-downloaded weekly as a consistency check. |
| Combine objects into one query | NORAD ids are sent as comma-delimited lists, not one request per satellite. |
| Back off on errors | 429/5xx responses get waits of 60 s or more, then give up after 4 attempts. |

`--force` skips the ledger checks. Use it only if you know the account hasn't
already queried that day. Don't run several accounts at once to get around the
limits; Space-Track forbids it.

## How "new" and "removed" are decided

* **New** means newly added to the public catalog (Space-Track's `DEBUT`
  timestamp). New launches usually take days to weeks to be cataloged, so a
  launch's objects can arrive across several reports. Occasionally objects that
  have long been in orbit (for example debris from old breakups) are cataloged for
  the first time; these are flagged *late catalog entry*.
* **Removed** means a re-entry recorded in the catalog (`satcat_change` decay
  date, with the precise epoch from `decay` messages). The weekly full re-sync
  also catches objects that left the public catalog without a reported decay.
* Daily queries overlap the previous run by a day and are de-duplicated, so a
  late or skipped run never loses or double-counts objects.

## Confidence ratings

Every reported fact carries its **source** and a **High / Medium / Low** rating
with a short reason. The email shows each object's overall rating (its weakest
catalog or orbit fact) as a signal-bar meter. The CSVs have `<field>_source`,
`<field>_confidence` and `<field>_basis` columns for every rated field. The rules
are in `gonk/confidence.py`:

| Source | Provides | How it is rated |
|---|---|---|
| Space-Track SATCAT | Name, country, type, launch | High by default. Medium for placeholder names (`OBJECT A`). Low when country is TBD or type is TBA/unknown. |
| Space-Track GP elements | Orbit | High: elements under 2 days old for an object tracked 3+ days. Medium: 2–7 days old, or the first days after launch. Low: older than 7 days or none published. |
| Derived by Gonk | Orbit class, SSO flag, constellation | Orbit class inherits the orbit's rating. Constellation comes from name matching, so it is at most Medium. |
| Space-Track decay messages | Re-entries, forecasts | High: re-entry recorded. Low: left the catalog with no decay reported. TIP forecasts Medium, 60-day forecasts Low. |
| SatNOGS DB (community) | Radio frequencies | High: confirmed and IARU-coordinated or ITU-notified. Medium: confirmed, not coordinated. Low: unconfirmed, a flagged frequency violation, or an object whose identity is still provisional. |

Community and inferred facts (radio frequencies, constellation) are rated
separately and never raise or lower an object's overall rating.

## Data notes and limits

* **Radio frequencies are not in Space-Track.** Gonk looks them up in the community
  [SatNOGS DB](https://db.satnogs.org) (one bulk download per day, cached). Many new
  satellites have no entry yet, and military or commercial payloads often never do.
  Set `GONK_SATNOGS=0` to turn this off.
* **Owner/operator and mission purpose** are not in Space-Track either. Gonk shows
  the registering country/organization and recognizes major constellations by name
  (`gonk/catalog.py`, `CONSTELLATIONS`).
* Orbits for brand-new objects come from their first element sets and refine over time.
* Numeric radar cross-sections are restricted; only the public size class
  (Small/Medium/Large) is shown.
* Check the Space-Track user agreement before redistributing its data widely.

## Development

```bash
python -m unittest -v        # offline tests; no network or account needed
```

Code layout: `gonk/spacetrack.py` (API client and usage rules), `gonk/pipeline.py`
(baseline and daily diff), `gonk/catalog.py` (normalization, orbit
classification), `gonk/report.py` (HTML/CSV), `gonk/emailer.py` (SMTP),
`gonk/satnogs.py` (radio frequencies), `gonk/confidence.py` (ratings). You can
also run any command as `python -m gonk ...` without installing.
