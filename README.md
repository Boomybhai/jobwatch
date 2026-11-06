# jobwatch

jobwatch checks the careers pages of the companies in `companies.csv` a few times a day. When one of them posts a **new** job whose title matches your keywords, it sends you an email. If you run it on your own laptop, it can also show a desktop pop-up.

```
jobwatch: 2 new postings at 2 companies

Dow Benelux B.V.
  - Graduate Process Engineer (Terneuzen)
    https://...
Tata Steel IJmuiden B.V.
  - Junior Procestechnoloog
    https://...
```

---

## How it works

1. **It finds the complete job list behind each careers page.** Many careers pages only show the first 10–25 jobs, so jobwatch looks for a fuller source, in this order:
   - **A job system's feed.** It checks the page for links to Workday, Greenhouse, Lever, Recruitee, SmartRecruiters, Workable, Personio, Oracle Cloud or Eightfold and reads that system's full JSON job list. This is how ASML is covered. SAP SuccessFactors sites (jobs.tetrapak.com, jobs.exxonmobil.com, ...) are read from their complete RSS job feed, and Radancy sites (careers.halliburton.com, ...) from their job sitemap.
   - **A "vacancies" link.** If a homepage or "working here" page shows no jobs, jobwatch follows its vacancies/careers link once and looks again there.
   - **Blocked pages.** If a site refuses plain requests (403, too many requests, certificate problems), jobwatch opens it in a real browser instead.
   - **A job sitemap.** This is the sitemap.xml that sites publish so Google can index every vacancy.
   - **The page itself,** following "next page" links. If the jobs only appear after JavaScript runs, it opens the page in a headless browser first.

   The source it picks is remembered in `state.json` and checked again every 14 days, or sooner if it breaks. To see which source each company uses, run `jobwatch discover` (or `python -m jobwatch discover`).
   For sitemap and page jobs that pass the title filter, jobwatch opens the job page once to read the exact title and city. That is what lets it drop jobs outside the country.
2. **It remembers what it has seen** in `state.json`. The first time it checks a company, it only takes note of the existing jobs and doesn't email you, so you aren't flooded on day one. After that, you only hear about postings that are new.
3. **It filters by job title** using `config.yaml`. For example, "graduate", "process engineer" and "procestechnoloog" are in; "senior", "operator" and "internship" are out. When a site gives a location, jobs that are clearly outside the Netherlands are dropped.
4. **It emails you** one digest per run, and only when something new has turned up.

---

## Setup: GitHub Actions (runs in the cloud, even when your laptop is off)

### 1. Create a Gmail app password

Google won't let scripts log in with your normal password.

1. Turn on 2-step verification for your Google account.
2. Go to <https://myaccount.google.com/apppasswords>, create a password called "jobwatch", and copy the 16 characters.

### 2. Put the project on GitHub

1. Create a new **private** repository, for example `jobwatch`.
2. Upload all the files in this folder, including the hidden `.github` folder. With git:
   ```bash
   cd jobwatch
   git init && git add . && git commit -m "jobwatch"
   git branch -M main
   git remote add origin https://github.com/<you>/jobwatch.git
   git push -u origin main
   ```

### 3. Add your email settings as secrets

In the repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Name            | Value                                       |
|-----------------|---------------------------------------------|
| `SMTP_USER`     | your Gmail address                          |
| `SMTP_PASSWORD` | the 16-character app password               |
| `EMAIL_TO`      | where alerts should go (can be the same)    |

You only need `SMTP_HOST` and `SMTP_PORT` if you're not using Gmail. The defaults are `smtp.gmail.com` and `465`.

### 4. Turn it on

1. Open the **Actions** tab and enable workflows if GitHub asks.
2. Click **jobwatch → Run workflow** once. This first run builds the baseline, so no email arrives.
3. After that it runs automatically at about 08:17, 14:17 and 20:17 Dutch time.
4. To change the times, edit the `cron:` line in `.github/workflows/jobwatch.yml`. Cron times are in UTC.

**Cost:** a run takes roughly 5–10 minutes. Three runs a day fits comfortably inside the 2,000 free minutes a month that private repositories get.

**Inactive repos:** GitHub pauses scheduled workflows in repositories with no activity for 60 days. The automatic `state.json` commits normally count as activity. If the workflow ever stops, click **Enable workflow** in the Actions tab.

---

## Desktop pop-ups on your laptop (optional)

Pop-ups can only appear on a computer that's switched on. That means they come from a copy of jobwatch running on your laptop, not from GitHub. The laptop copy keeps its own `state_local.json`, so it doesn't clash with the cloud copy.

```powershell
cd jobwatch
pip install -r requirements.txt
python -m playwright install chromium
python -m jobwatch run --notify desktop --state state_local.json     # try it once
powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1   # every 3 hours
```

To get emails from the laptop copy as well, use `--notify desktop,email` and set the `SMTP_*` environment variables in Windows.

---

## Windows .exe (no Python needed)

You can build `jobwatch.exe` from this project. PyInstaller can only build Windows programs on Windows, so there are two ways to do it:

- **Let GitHub build it (easiest).** Once the project is on GitHub, open **Actions → build-exe → Run workflow**. After about 5 minutes, download **jobwatch-windows** from that run's *Artifacts* section.
- **Build it on your laptop.** You need Python installed. Then run `powershell -ExecutionPolicy Bypass -File packaging\build_exe.ps1`. The result is `dist\jobwatch-windows.zip`.

Unzip the result anywhere and read `HOW-TO-USE.txt`. In short:

1. Double-click `jobwatch.exe` to check now.
2. Run `jobwatch.exe install-task` once to have it check every 3 hours with Windows notifications.
3. Optionally, rename `email.env.example` to `email.env` and fill it in to get emails as well.

`companies.csv`, `config.yaml` and `state.json` sit next to the .exe, so you can edit them without rebuilding. For pages that need JavaScript, the .exe uses the Microsoft Edge that's already on your PC. Windows SmartScreen may warn the first time you run it, because the .exe isn't code-signed: click **More info → Run anyway**.

---

## Phone notifications (no email account needed)

1. Install the free **ntfy** app on your phone (Play Store / App Store).
2. In the app tap **+** and subscribe to a private, hard-to-guess name, e.g. `your-private-topic-name-x7q2`.
3. Put the same name in `email.env` (next to `config.yaml`): `NTFY_TOPIC=your-private-topic-name-x7q2`
4. Test: `python -m jobwatch test-phone`
5. Run with `--notify desktop,phone`. (For GitHub Actions add an `NTFY_TOPIC` secret.)

Anyone who knows the topic name can read it, so don't use your real name alone.

---

## Day-to-day use

| What you want                                 | What to do |
|-----------------------------------------------|------------|
| Different job titles                          | Edit `include_keywords` and `exclude_keywords` in `config.yaml` |
| Only check the big employers                  | Set `min_priority: High` in `config.yaml` |
| Add a company                                 | Add a row to `companies.csv`. Only `name` and `url` are required |
| Stop checking a company                       | Set `enabled` to `no` |
| A page shows 0 jobs                           | Open it in your browser. If the jobs only appear after it loads, set `render` to `always`. If there's a better jobs-list URL, put that in `url` |
| See which method each company uses            | `python -m jobwatch list` |
| Test one company without sending anything     | `python -m jobwatch run --only "tata" --dry-run -v` |
| Check that email works                        | `python -m jobwatch test-email` |

Every 28 runs (about every 9 days), the email also lists any careers pages that have failed three times in a row, so you know which links to fix.

### Columns in `companies.csv`

`name, priority, group, url, enabled, adapter, render, link_type, note`

- `adapter`: leave this as `auto`. jobwatch works out which kind of site it is from the URL. You can force one of `workday`, `greenhouse`, `recruitee`, `smartrecruiters`, `workable`, `lever`, `personio`, `oracle`, `successfactors`, `eightfold`, `sitemap` or `html`.
  - `successfactors`: add `?employer=Name` to the URL to keep only one employer's jobs on a site shared by a group (see DFE Pharma).
  - `eightfold`: the URL needs `?domain=<company domain>`; add `&location=Netherlands` to only read jobs in one country.
- `render`: `auto` uses headless Chrome only when the plain page shows no job links. `always` and `never` do what they say.

---

## Limits to know about

- **The first run after updating is slow.** Expect 45–90 minutes while it finds every company's job source. It doesn't alert you during this run. Later runs reuse what it found and are faster.

- **48 companies are switched off.** Their spreadsheet link points to a job board such as LinkedIn, Indeed or Glassdoor. These are not scraped, because their terms forbid it and they block bots. Alternatively, the company has no known careers page. If you find a company's own careers URL, paste it into `url` and set `enabled` to `yes`.
- **Some links go to a careers landing page rather than the job list.** Those pages may show few or no jobs. The fix is to open the page, click through to the actual vacancy list, and use that URL instead.
- **The plain-HTML method is a heuristic.** It treats links that look like job postings as jobs. It works on most server-rendered careers sites, but a redesign of a site can break it. The "failing pages" list in the email points you to those.
- jobwatch waits 2 seconds between requests and follows each site's `robots.txt`. Keep it at a few runs a day.

## Run the tests

```bash
pip install pytest
python -m pytest -q
```
