# ISSA / ICSA Sailing Data

Scraped from [scores.hssailing.org](https://scores.hssailing.org) (ISSA) and
[scores.collegesailing.org](https://scores.collegesailing.org) (ICSA) using `scraper.py`.

---

## Directory Layout

```
db/           SQLite databases (source of truth)
data/         CSV exports of every database table
issa_logos/   School burgee images — ISSA (428 schools)
icsa_logos/   School burgee images — ICSA (194 schools)
```

---

## Databases (`db/`)

### `issa_results.db` — ISSA Fleet & Keelboat Racing

Covers **f13–s26** (26 seasons, 3,571 regattas).

#### `race_results` — 600,288 rows
One row per boat per race in every dinghy fleet-race regatta.

| Column | Description |
|--------|-------------|
| `season` | Season code, e.g. `f24`, `s25` |
| `regatta_name` | Human-readable regatta name |
| `regatta_url` | URL path used as a stable ID, e.g. `/f24/mcmillan` |
| `boat` | Boat class, e.g. `C420`, `CFJ` |
| `division` | `A` or `B` |
| `race_num` | Race number within the regatta |
| `place` | Finishing place (999=DNF, 998=DNS, 997=DNC, …) |
| `school` | Full school name |
| `school_slug` | URL slug, e.g. `annapolis` |
| `team` | Team name (usually same as school) |
| `skipper` | Skipper full name |
| `skipper_grade` | Graduation year, e.g. `'26` |
| `crew` | Crew full name (blank if singlehanded) |
| `crew_grade` | Crew graduation year |

#### `keelboat_results` — 4,892 rows
One row per boat per race in keelboat regattas (J22, J70, Sonar, IOD, Etchells, etc.).
Same columns as `race_results` minus sailor data (sailors stored separately).

#### `keelboat_sailors` — 1,270 rows
Sailors for keelboat regattas.

| Column | Description |
|--------|-------------|
| `regatta_url` | Links to `keelboat_results.regatta_url` |
| `school_slug` | School slug |
| `school` | Full school name |
| `team` | Team name |
| `division` | Division sailed |
| `sailor_name` | Sailor full name |
| `grade` | Graduation year |
| `races` | Race numbers sailed, e.g. `1-3,5` |

---

### `issa_tr_results.db` — ISSA Team Racing

Covers **f14–s25** (24 seasons, 445 regattas).

#### `tr_standings` — 3,271 rows
Final standings for each school at each team-race regatta.

| Column | Description |
|--------|-------------|
| `season` | Season code |
| `regatta_name` | Regatta name |
| `regatta_url` | Stable ID |
| `boat` | Boat class |
| `place` | Final place |
| `school` / `school_slug` | School identity |
| `team` | Team name |
| `wins` / `losses` | Win-loss record |
| `win_pct` | Win percentage |

#### `tr_sailors` — 10,392 rows
Skipper/crew pairs that sailed for a school at a team-race regatta (overall roster, not per-match).

| Column | Description |
|--------|-------------|
| `regatta_url` | Links to `tr_standings` |
| `school_slug` | School |
| `team` | Team name |
| `skipper` / `skipper_grade` | Skipper name and grad year |
| `crew` / `crew_grade` | Crew name and grad year |

#### `tr_matches` — 16,963 rows
One row per individual match in a team-race regatta.

| Column | Description |
|--------|-------------|
| `regatta_url` | Regatta ID |
| `round_num` | Round within the regatta |
| `match_num` | Match number |
| `boat` | Boat class |
| `winner_school` / `winner_slug` / `winner_team` | Winning school |
| `winner_positions` | Finishing positions, e.g. `1-2-4` |
| `loser_school` / `loser_slug` / `loser_team` | Losing school |
| `loser_positions` | Finishing positions, e.g. `3-5-6` |

#### `tr_match_sailors` — 82,840 rows
Sailors assigned to each boat in each match.

| Column | Description |
|--------|-------------|
| `regatta_url` | Regatta ID |
| `match_num` | Match number |
| `side` | `winner` or `loser` |
| `boat` | Boat letter (`A`, `B`, `C`) |
| `school_slug` / `team` | School identity |
| `skipper` / `skipper_grade` | Skipper |
| `crew` / `crew_grade` | Crew |

---

### `icsa_results.db` — ICSA Fleet & Keelboat Racing

Covers **f08–s26** (36 seasons, 3,759 regattas).
Same schema as `issa_results.db` with one addition:

- `race_results` has an extra `keelboat` column (boolean flag) indicating whether the row
  came from a keelboat regatta (Catalina 37, Navy 44, J-70, etc.).

Row counts: **876,936** fleet race rows · **11,778** keelboat results · **5,272** keelboat sailors.

---

### `icsa_tr_results.db` — ICSA Team Racing

Covers **f12–s26** (27 seasons, 658 regattas).
Identical schema to `issa_tr_results.db`.

Row counts: **4,901** standings · **17,895** sailors · **32,062** matches · **177,068** match sailors.

---

### `sailors.db` — Cross-Season Sailor Roster

Built from school roster pages. Covers all available seasons (**f08–s26**, 31 seasons total).
ICSA spring rosters are unavailable on the source site; only fall seasons are present for ICSA.

#### `sailors` — 54,548 rows (26,477 ISSA · 28,071 ICSA)

| Column | Description |
|--------|-------------|
| `id` | Auto-increment primary key |
| `name` | Sailor full name (normalised whitespace) |
| `school_slug` | School URL slug |
| `site` | `issa` or `icsa` |
| `class_year` | Graduation year (most recent known) |

#### `sailor_seasons` — 216,942 rows
Join table linking each sailor to every season they appeared on a school roster.

| Column | Description |
|--------|-------------|
| `sailor_id` | References `sailors.id` |
| `season` | Season code, e.g. `f24` |

---

## CSV Exports (`data/`)

Every database table is exported to CSV after each scrape run.
CSVs are flat copies — all columns match the database schema above.

| File | Source table |
|------|-------------|
| `issa_results.csv` | `issa_results.db › race_results` |
| `issa_keelboat_results.csv` | `issa_results.db › keelboat_results` |
| `issa_keelboat_sailors.csv` | `issa_results.db › keelboat_sailors` |
| `issa_tr_standings.csv` | `issa_tr_results.db › tr_standings` |
| `issa_tr_sailors.csv` | `issa_tr_results.db › tr_sailors` |
| `issa_tr_matches.csv` | `issa_tr_results.db › tr_matches` |
| `issa_tr_match_sailors.csv` | `issa_tr_results.db › tr_match_sailors` |
| `issa_schools.csv` | School listing (name, slug, city, state, logo, conference) |
| `icsa_results.csv` | `icsa_results.db › race_results` |
| `icsa_keelboat_results.csv` | `icsa_results.db › keelboat_results` |
| `icsa_keelboat_sailors.csv` | `icsa_results.db › keelboat_sailors` |
| `icsa_tr_standings.csv` | `icsa_tr_results.db › tr_standings` |
| `icsa_tr_sailors.csv` | `icsa_tr_results.db › tr_sailors` |
| `icsa_tr_matches.csv` | `icsa_tr_results.db › tr_matches` |
| `icsa_tr_match_sailors.csv` | `icsa_tr_results.db › tr_match_sailors` |
| `icsa_schools.csv` | School listing |
| `sailors.csv` | `sailors.db › sailors` joined with binary season columns |

### `sailors.csv` format

Each row is a unique sailor. After `name, school_slug, site, class_year` there is one binary
column per season (`s14`, `f14`, … `s26`). A value of `1` means that sailor appeared on that
school's roster in that season; `0` means they did not.

```
name,school_slug,site,class_year,f08,f09,...,f24,s25,f25,s26
Blake Gezelle,annapolis,issa,2027,0,0,...,1,1,1,1
```

---

## Logo Images

School burgee/logo images downloaded from each site's `/schools/{slug}/` page.

- **`issa_logos/`** — 428 PNG files, named `{school_slug}.png`
- **`icsa_logos/`** — 194 PNG files, named `{school_slug}.png`

135 ISSA schools and 3 ICSA schools have no logo on the source site.

---

## Website Generators

The static website is built by two parallel generators that produce **identical page structures**
for the two leagues. There is deliberate duplication by design — each generator is fully
self-contained so either site can be rebuilt independently. If you add a feature to one, mirror it
in the other.

### Generator scripts

| Script | League | Output directory | Database(s) used |
|--------|--------|-----------------|-----------------|
| `generate_site.py` | ICSA | `website/icsa/` | `icsa_results.db`, `icsa_tr_results.db`, `sailors.db` |
| `generate_issa_site.py` | ISSA | `website/issa/` | `issa_results.db`, `issa_tr_results.db`, `sailors.db` |

### Shared assets

Both sites share a single CSS file and a single JS file through **relative symlinks**:

```
website/icsa/styles.css  →  ../styles.css      (root styles.css)
website/issa/styles.css  →  ../styles.css      (same file)
```

The symlink for `website/issa/styles.css` must exist for ISSA school and sailor pages to render
correctly (they reference `../../styles.css` relative to their own depth). If it ever gets lost:

```bash
ln -s ../styles.css website/issa/styles.css
```

### Page structure (both leagues identical)

```
website/{league}/
  index.html                      Season index
  {season}/
    index.html                    Season overview (regattas list)
    {regatta-slug}/
      index.html                  Regatta result page (fleet / keelboat / team-race)
  schools/
    index.html                    Schools list + Leaflet map
    {school-slug}/
      index.html                  School page (info, roster by class year)
  sailors/
    {sailor-slug}/
      index.html                  Sailor page (career history)
  conferences/                    (ICSA only — MAISA, NEISA, PCCSC, …)
    {conf-slug}/index.html
  districts/                      (ISSA only — NESSA, MASSA, SAISA, …)
    {district-slug}/index.html
```

### Rebuilding the site

```bash
# Full rebuild (all seasons)
python3 generate_issa_site.py 2>&1 | tee generate_issa_site.log
python3 generate_site.py      2>&1 | tee generate_site.log

# Schools + conferences/districts only (fast, ~10 s)
python3 generate_issa_site.py --schools-only 2>&1 | tee generate_issa_site.log
python3 generate_site.py      --schools-only 2>&1 | tee generate_site.log
```

### Key shared helpers (both generators)

| Function | Purpose |
|----------|---------|
| `school_link(school, slug, prefix)` | Wraps school name in `<a>` to its school page |
| `sailor_slug(name, school_slug)` | Deterministic URL slug for a sailor |
| `lead_cell(school, slug, depth)` | Burgee `<img>` cell for league-summary tables |
| `build_schools_page()` | Generates `schools/index.html` with Leaflet map |
| `_school_page_html(...)` | Generates an individual school page |
| `build_fleet_regatta_page(...)` | Score-summary + per-division tabs + sailors tab |
| `build_tr_regatta_page(...)` | Team-race standings + match-results tab |
| `build_keelboat_regatta_page(...)` | Keelboat summary + per-division tabs |

### Map (schools/index.html)

Both `build_schools_page()` functions embed a **Leaflet.js** interactive map directly into
`schools/index.html`. The map data (lat/lon, conference/district, name) is read from
`data/venues.csv` at build time and baked in as a JSON blob. Schools are colour-coded by
conference/district; selecting one from the dropdown zooms the map to fit that group using
`fitBounds`.

If school coordinates are wrong, edit `data/venues.csv` and set `geocode_source=manual`
to prevent the next scrape from overwriting them. Four Canadian schools required manual fixes
(Nominatim geocoded them to US cities with the same name):

| School | Correct coords |
|--------|---------------|
| Queen's University | 44.2253, -76.4951 (Kingston, ON) |
| University of Toronto | 43.6629, -79.3957 (Toronto, ON) |
| University of Victoria | 48.4634, -123.3117 (Victoria, BC) |
| UBC | 49.2606, -123.2460 (Vancouver, BC) |

---

## Scraper (`scraper.py`)

```bash
# Scrape all regattas (all seasons)
python3 scraper.py --site issa
python3 scraper.py --site icsa

# Scrape sailor rosters (all seasons)
python3 scraper.py --rosters

# Scrape rosters for specific seasons only
python3 scraper.py --rosters --seasons s25 f25

# Wipe and re-scrape a single season (rosters + regattas)
python3 scraper.py --refresh-season s26
python3 scraper.py --refresh-season s26 --site issa

# Download school logos
python3 scraper.py --logos
python3 scraper.py --logos --site icsa

# Export schools CSV
python3 scraper.py --schools
```
