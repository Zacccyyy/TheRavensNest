# The Raven's Nest 🐦‍⬛

A **local-first workshop inventory manager**. One command bar, a phone
capture flow, QR-labelled bins, projects with BOMs, and supplier basket
optimisation — all running on your own machine, with your data in plain
files you can read, back up, and sync yourself.

**The app is public. Your inventory is private.** Cloning this repository
gives you the software with an *empty* inventory — your items, photos,
and history are never part of this repo, and nobody who downloads the app
can see anyone else's data. See [Your data stays yours](#your-data-stays-yours).

---

## Quick start

You need [uv](https://docs.astral.sh/uv/) (which brings its own Python) and
git. Install uv if you don't have it:

```sh
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
irm https://astral.sh/uv/install.ps1 | iex
```

Then clone, install, and run — same on every platform:

```sh
git clone https://github.com/Zacccyyy/TheRavensNest.git ravens-nest
cd ravens-nest
uv sync
uv run uvicorn ravens_nest.app:app
```

Open **<http://127.0.0.1:8000>** and you'll land on the command bar with an
empty inventory — a banner offers a guided setup (describe your shelving,
print bin labels, pick suppliers). Type `help` any time for every command.

Photo AI identification is optional; without a key, captures become blank
cards you fill in by hand. To enable it, copy `.env.example` to `.env` and
add your [Anthropic API key](https://platform.claude.com/). To use it from
your phone, see [Phone access](#phone-access-ios-safari--android).

Runs on macOS (Intel & Apple Silicon), Windows, and Linux. Detailed
walkthrough below.

---

## What it does

- **Command bar** — the whole interface is one input: search, bin
  lookups, moves, builds, recounts, reordering. Keyboard-first.
- **Photo capture + AI identification** — snap a part on your phone;
  Claude vision extracts name/part number/quantity with confidence
  ratings and asks targeted questions about what it can't see. You
  confirm every card — it never guesses silently.
- **QR bin labels + scanning** — printable Avery-style label sheets,
  camera scanning (works on iOS Safari), USB barcode scanner support.
- **Projects & BOMs** — import a BOM CSV, match lines to your items
  (learning aliases as you go), reserve stock, build ×N with exact
  shortage checks, un-build to return parts, per-line and total costing.
- **Sourcing** — supplier links per item, an auto-populated reorder
  basket, on-demand price checks (no background scraping), and candidate
  order baskets: cheapest / fewest suppliers / fastest.
- **Event-sourced** — every change is an append-only event in a plain
  `.jsonl` file. The SQLite database is a disposable cache rebuilt from
  the log. Optional Git-based sync between your own machines.

## Requirements

| What | Why |
|---|---|
| **Python 3.11+** | the app (3.12 works fine) |
| **[uv](https://docs.astral.sh/uv/)** | dependency management — installs everything else |
| **git** | version control; also powers optional multi-machine sync |
| **Anthropic API key** *(optional)* | only for photo identification; everything else works without it |

Runs on Windows, macOS, and Linux. The phone UI targets iOS Safari and
Android browsers over your home network.

## Install & first run

```sh
# 1. Get uv (skip if you have it)
#    Windows (PowerShell):
irm https://astral.sh/uv/install.ps1 | iex
#    macOS/Linux:
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Get the app
git clone https://github.com/Zacccyyy/TheRavensNest.git ravens-nest
cd ravens-nest

# 3. Install dependencies
uv sync

# 4. Run it
uv run uvicorn ravens_nest.app:app
```

Open <http://127.0.0.1:8000> — you'll land on the command bar with an
empty inventory. Your data files are created automatically under `data/`
the first time you add anything.

**Optional — photo identification:** copy `.env.example` to `.env` and
put your Anthropic API key in it:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Without a key, captured photos still queue for review with blank cards
you fill in by hand; nothing breaks.

### Phone access (iOS Safari / Android)

Serve on your LAN and open it from your phone:

```sh
uv run uvicorn ravens_nest.app:app --host 0.0.0.0 --port 8080
# then on the phone:  http://<your-computer-name>.local:8080/m
```

`/m` is the thumb-first phone UI: a big **Capture item** button (native
camera — works over plain HTTP on iOS), **Scan location label** (takes a
still photo and decodes the QR on the phone, so no HTTPS is needed), and
quick search. If the server is briefly unreachable, captures queue in the
browser and retry automatically.

> **Note on live camera scanning:** the desktop move console (`/move`)
> can also scan continuously through the camera. iOS only allows *live*
> video capture on HTTPS or localhost — that's an Apple restriction — so
> on a phone over plain LAN HTTP, use `/m`'s still-photo scan (fully
> supported) or a USB/Bluetooth barcode scanner (types + Enter, no
> drivers, works everywhere).

> **Security note:** the server has **no login**. By default it binds to
> `127.0.0.1` (your machine only). Only use `--host 0.0.0.0` on a network
> you trust (your home LAN) — anyone who can reach the port can use the
> app and see your inventory. Don't port-forward it to the internet.

---

## Your data stays yours

The code and your inventory are **fully separated**:

```
ravens-nest/                  ← this repo: code only, share it freely
├── ravens_nest/              the app
├── tests/
└── data/                     ← YOUR inventory. Gitignored. Never leaves
    ├── events/YYYY-MM.jsonl     your machine via this repo.
    ├── assets/<sha256>.jpg      item photos
    ├── inbox/                   photo drop folder
    ├── pending/                 capture cards awaiting review
    └── cache.db                 SQLite cache — disposable, rebuilt from events
```

`data/` is in `.gitignore`, so `git push` on this repo can never publish
your inventory, and `git pull` (updating the app) can never touch it.

**Anyone who wants their own inventory just clones the repo and starts
using it** — their `data/` files are created on first use, on their
machine, private to them. There is nothing to configure and no way for
one person's clone to see another person's data.

### Optional: sync *your* inventory between *your* machines

The app can commit and push your event log to a Git remote — use a
**separate, private repository** for that (never this public one):

```sh
# 1. Make a private data repo (once) — e.g. on GitHub, create
#    "ravens-nest-data" and mark it PRIVATE. Then:
git clone <your-private-data-repo-url> %USERPROFILE%\ravens-nest-data   # Windows
git clone <your-private-data-repo-url> ~/ravens-nest-data               # macOS/Linux

# 2. Point the app at it before starting the server:
#    Windows (PowerShell):
$env:RAVENS_NEST_DATA = "$env:USERPROFILE\ravens-nest-data\data"
#    macOS/Linux:
export RAVENS_NEST_DATA=~/ravens-nest-data/data

uv run uvicorn ravens_nest.app:app
```

With that set, the app: pulls on startup and replays new events into the
cache; batches every write into a debounced commit+push (~10s); exposes
`POST /sync` and `GET /sync/status`; and auto-resolves the append-only
log's merge conflicts by union. Do the same on a second machine and both
converge. Set the variable permanently with `setx RAVENS_NEST_DATA ...`
(Windows) or in your shell profile.

Without `RAVENS_NEST_DATA`, data lives in `./data` inside the app folder:
completely private, local-only, and untouched by app updates — sync
status will simply report nothing to push.

### Updating the app

```sh
git pull
uv sync
```

Your data directory is never modified by an update. The SQLite cache can
always be rebuilt from your event log: `uv run python -m ravens_nest.replay`.

---

## The command bar

The primary interface is one input at `/` — no navigation tree:

| You type | You get |
|---|---|
| `3mm heat shrink` | fuzzy search (name, part number, description, aliases) — "A-2-3b, 12 left, 4 free (8 reserved)" |
| `all: heat shrink` | search including zero-quantity items |
| `archived: servo` | search retired items (they appear nowhere else) |
| `A-2-3b` | what's in that bin (zero-qty greyed, still listed) |
| `move to A-2-3b` | move mode: scan/type items, exact matches move immediately |
| `build RPSRobot x2` | build confirmation with needs and shortages, then Confirm |
| `need 20 more m3` | adds to the reorder basket (asks if ambiguous) |
| `recount A-2-3b` | bin recount form — unchanged counts emit nothing |
| `low` | everything with free stock under its min |
| `history A-2-3b` | event history for a bin (or an item) — paginated, filterable |
| `undo` | reverse your last action (`undo list` browses the last 20) |
| `health` | data-quality score with itemised, fixable counts |
| `merge` | scan for likely duplicate items |
| `price basket` | on-demand basket pricing |
| `help` | every command with examples; `help <verb>` for detail |

Results appear as you type (read-only lookups render live; actions show a
"press Enter" hint and only execute on Enter). Arrow keys move the
selection, Enter acts, Esc clears. Ambiguous commands always ask instead
of guessing, and error messages say what was expected with a valid
example ("Unknown location 'A-2-9' — Unit A shelf 2 has bins 1-6").

**Zero vs archived**: an item at qty 0 is still an item you own — it
keeps its bin, history, prices, links, and reorder logic, and is merely
hidden from plain search (always with a "N zero-qty hidden" count).
Archiving is genuine retirement: excluded from search, reorder, and BOM
matching entirely, reachable only via `archived:`, reversible any time.

**Undo**: every state-changing action gets a compensating event —
nothing is ever deleted. The undo stack is per-machine (one computer
never blind-undoes another's work), action toasts carry a one-click
undo, undoing an undo redoes, and genuinely unsafe inverses (the item
moved again since) are refused with an explanation and the manual path.

Item cards (`/items/<id>`) show the photo, every field, stock vs
reservations by project, supplier links, quick-edit fields, archive
controls, and the item's narrated event history ("Moved A-1-2 → A-2-3b",
"Consumed 2 by build RPSRobot ×1") with actor and timestamp — including
the history of anything merged into it.

## First-run setup, help, and health

A fresh install offers a **guided setup** (`/setup`): describe your
shelving in a form *or* plain language ("3 shelving units, 5 shelves
each, 6 bins per shelf, deep bins have a front and back"), preview every
generated location ID, create them, print the labels, tick the suppliers
you actually use, and get a plain explanation of what the API key adds.
Skippable, resumable, re-runnable — adding another shelving unit later is
the same flow.

`health` scores your data quality and itemises what needs attention —
items with no location/price/photo/min-qty/supplier link, stale prices
(configurable via `RAVENS_NEST_PRICE_STALE_DAYS`), items untouched for a
year (recount prompt), unresolved BOM lines, empty bins, likely
duplicates, unpushed sync events — each entry a clickable list with a
fix flow.

## Duplicates and merging

At capture-confirm time, a fuzzy check across names, part numbers, and
aliases surfaces near-matches *above* the confirm button — "You have
'SG90 Micro Servo' in A-1-2 (qty 8) — same thing? [Merge — adds qty]".
The same check runs on CSV import. `merge` (or `/merge`) scans the whole
inventory for likely duplicate pairs. Merging sums quantities, transfers
aliases/links/photo/reservations, makes the source's name an alias on
the target, keeps the source's history readable from the target, and
archives the source — never deletes. Different unit types require an
explicit confirmation; different bins require you to choose which
location is correct. Every merge is undoable.

## CSV import & export

`/import` takes a CSV (`name` required; `part_number, description,
unit_type, qty, min_qty, location, last_paid_aud, manufacturer,
package_type, supplier_url` optional) and always shows a **dry-run
preview** first: N new, N matched, N ambiguous (with per-row decisions),
N errors with row numbers and reasons. Nothing is written until
confirmed, everything is normal events, and resolutions store aliases so
re-importing the same sheet matches clean.

Export any time: `/export/items.csv` (same columns;
`?include_archived=1` for retired items) or `/export/full.zip` — the
complete event log + photos with restore instructions, so you can leave
with everything.

## Multi-item capture & item labels

One photo of a drawer with six different parts produces six review
cards — each with its own fields, confidence ratings, questions, and a
"where in the photo" hint, all sharing the one content-addressed photo.
Cards are dismissed individually. If the model can't confidently
separate items it returns one card and says so — it never splits
speculatively.

`/labels/items` prints item labels (name, qty, home bin, QR) — one, a
bin's worth, or a selection — in a smaller 4-across format. Item QRs
encode `RN-ITEM:<id>` and location QRs `RN-LOC:<id>`, so scans are never
ambiguous (bare old-format location labels still scan fine). Scanning an
item label jumps straight to its card.

## Photo capture & identification

Two ingest paths feed one pipeline: `POST /capture` (the phone button)
and the inbox folder (`data/inbox/*.jpg`, scanned on startup and via
`POST /inbox/scan` — point `RAVENS_NEST_INBOX` at an iCloud/Dropbox
folder for offline capture; files are consumed on ingest).

Photos are stored content-addressed at `data/assets/<sha256>.jpg` and
deduplicated by hash — one photo, one vision call, ever. The model is
instructed never to guess: unknown fields come back null with a targeted
question ("M3 screw, length unknown — what length?"). API failures
degrade to a blank card so the queue keeps working offline. Review at
`/queue`: confidence-flagged fields, inline questions, confirm to create
the item or merge into an existing one. Model configurable via
`RAVENS_NEST_VISION_MODEL` (default `claude-opus-4-8`).

## Locations & movement

- **`/labels`** — printable QR label sheets (2×2in grid, Avery
  22806-style), with a batch generator for a whole unit (shelves × bins ×
  optional sections). Location IDs look like `A-2-3b`: unit letter, shelf
  from bottom, bin from left, optional lowercase section.
- **`/move`** — scan-driven move console: scan a bin, then rattle through
  items; exact scans move immediately, name searches show a pick list
  with bulk checkboxes. Scanning a freshly printed label auto-creates the
  location.
- **`/locations`** — units → shelves → bins tree with counts, per-bin
  contents, and empty-bin ("free space") detection.

## Projects & BOMs

Create a project, import a BOM CSV
(`part_number, description, quantity, unit[, reference_designators, notes]`).
**Import reserves, building consumes.** Lines match through a ladder —
exact part number → exact name → stored alias → scored fuzzy suggestions
you confirm by hand. Resolving a line once stores the BOM's text as an
alias, so the next revision matches automatically.

Stock semantics: `free_stock = qty_on_hand − Σ(active reservations)`.
"Build ×N" is rejected up front with an exact shortage list if free stock
can't cover it (a project's own reservations don't block it); "Un-build
×N" returns stock. The BOM table shows unit/extended cost from each
item's last paid price and flags unpriced lines.

## Sourcing & reordering

Seed the standard suppliers (Core Electronics, element14, RS Components,
DigiKey, Mouser, AliExpress, Bunnings, Jaycar) at `/suppliers`.
Reliability ratings (1–5) are **set manually by you** after orders arrive
— never scraped, never inferred. Each item takes supplier links (URL,
SKU, pack quantity).

The reorder basket (`/reorder`) auto-fills from free stock below
`min_qty` plus reservation shortfalls; add anything manually on top.
Suggested quantities are pack-aware (need 7 of a pack-of-10 part → one
pack of 10) and unit-type-aware (`each` rounds up to whole units; g/mm/mL
reorder in native amounts). **Price the basket** fetches your stored
product URLs on demand — extraction failures fall back to the last known
price marked stale, never invented — then shows candidate baskets:
cheapest total, fewest suppliers, fastest, each with per-supplier
subtotals, free-shipping thresholds applied, coverage, lead time, and
mean reliability. Recording a received order (`/orders/receive`) sets
last-paid prices, adds the stock, and prompts for the reliability rating.

## How it works

Every change is an event — one JSON object per line, appended to
`data/events/YYYY-MM.jsonl`:

```json
{"id": "<uuid>", "ts": "<ISO8601 UTC>", "actor": "<hostname>", "type": "item.created", "payload": {}}
```

The SQLite cache (`data/cache.db`) is rebuilt by replaying events sorted
by `(ts, id)`; replay is idempotent and deterministic. Quantities are
exact decimal strings end to end — never floats. Because the log is
append-only, Git merges of two machines' histories are clean unions.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `RAVENS_NEST_DATA` | `./data` | where your inventory lives (set it to a private data repo's `data/` dir to enable sync) |
| `RAVENS_NEST_REPO` | parent of data dir | the git repo used for sync |
| `RAVENS_NEST_INBOX` | `<data>/inbox` | watched photo drop folder |
| `RAVENS_NEST_VISION_MODEL` | `claude-opus-4-8` | model for photo identification |
| `RAVENS_NEST_DEBOUNCE` | `10` | seconds to batch writes into one sync commit |
| `ANTHROPIC_API_KEY` | — | photo identification (goes in `.env`) |

## Commands

```sh
uv run uvicorn ravens_nest.app:app            # serve (add --host 0.0.0.0 for LAN)
uv run python -m ravens_nest.replay           # rebuild cache.db from the event log
uv run python -m ravens_nest.sync             # one manual sync, prints status JSON
uv run pytest                                 # run the test suite
```
