# CargoShield demo

A working demo of a trust and risk layer for a cargo airline. One engine, three features:

1. **Verify.** Anyone sends an air waybill (AWB) number, a photo of the paperwork, or scans the QR on a cover certificate, and gets back GENUINE, NOT FOUND or SUSPICIOUS in seconds.
2. **Screen.** Every booking gets a risk score from 0 to 100 with plain-English reasons before it is accepted. HIGH goes to a person. Nothing is auto-rejected.
3. **Cover.** LOW and MEDIUM bookings get a top-up cargo insurance quote priced from that same score, with the full working shown.

It runs on a laptop, offline, from one command. The 3-minute click path for the CEO is in [DEMO.md](DEMO.md).

## What is synthetic

Everything. There is no connection to any airline system.

| Thing | How it is made |
|---|---|
| Airline | "Demo Airline", AWB prefix `999` (a placeholder, set in `config/settings.yaml`) |
| Routes | `data/routes.csv`: Nairobi, Johannesburg and Hong Kong hubs plus African destinations. Days and times are placeholders. **Refresh this file from the airline's public schedule page before any real pitch.** |
| Shippers (~400) | Invented company names. Emails use the reserved `.example` domain. Phone numbers put a `0` straight after the country code, a form the numbering plans in the demo countries don't issue, so none should reach a real phone. Any resemblance to a real business is chance. |
| Bookings (3,000) | Generated with fixed random seeds, so the demo is the same every time. About 4% carry a planted risk pattern with a known answer, used to test the engine. |
| Fraud ring | Five planted shippers sharing a phone, an email and a payer account. |
| Fake documents | 8 images in `data/fakes/`, drawn with Pillow. Each uses an AWB number that fails the check digit or does not exist. Scam *patterns* only (clearing-fee notes, release stamps, look-alike QR sites). Nothing copied from real forgeries, scam reports or court cases. |
| Insurance prices | Illustrative. Not real rates and not an offer of insurance. The UI says so. |

The UI carries the label "Demo data built on the public flight schedule". No real airline branding is used.

## Setup

You need Python 3.11 or newer.

```bash
cd cargo-shield
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
make install                                           # or: pip install -r requirements.txt
```

Photo reading uses the Tesseract OCR program, installed separately:

- Mac: `brew install tesseract`
- Ubuntu/Debian: `sudo apt install tesseract-ocr`
- Windows: the UB Mannheim installer, then make sure `tesseract` is on your PATH

Without Tesseract the app still works; when someone sends a photo, the chat asks them to type the number instead. QR codes are read with `zxing-cpp` (a normal pip package, already in requirements).

Then check everything is in place:

```bash
make doctor
```

## Commands

| Command | What it does |
|---|---|
| `make demo` | Seed, train and start the app at http://localhost:8000. The one command for the demo. |
| `make seed` | Same as `python -m cargoshield.seed --bookings 3000`. Builds a fresh database, writes the fake documents, trains the anomaly model, scores every booking and saves a reset snapshot. Takes about 10 seconds. |
| `make run` | Start the app on the existing database. |
| `make reset` | Put the database back to the demo starting state (issued cover, reviews, chat checks and live bookings are cleared). There is also a Reset button in the UI. |
| `make test` | Run the test suite (about 15 seconds). |
| `make report` | Print how many planted risks the engine caught and the false-positive rate on clean bookings. |
| `make train` | After editing `config/rules.yaml`: go back to the demo starting state, retrain, rescore and save that as the new starting state (so Reset keeps your tuning). A running app picks up the new rules and model on its next request. |
| `make doctor` | Pre-flight check. |

No `make` (e.g. Windows)? Use `python -m cargoshield demo`, `python -m cargoshield reset`, `python -m cargoshield test` and so on.

To let a phone on the same Wi-Fi scan the QR codes, start with `make demo HOST=0.0.0.0` and open the dashboard on the LAN address it prints. By default the app only listens on the laptop itself. Even on the network, other devices can only reach the public `/verify/...` pages; the dashboard and its API answer only the presenter's laptop, and writes from other websites are refused.

## How it works

```
data/routes.csv ─► seed.py ─► SQLite ─► risk.py (rules.yaml + IsolationForest) ─► risk_score
                                │             ▲
                                │       graph.py (shared phones, emails, payer accounts)
                                ▼
       verify.py ◄── messaging.py ◄── chat simulator (later: whatsapp.py)
       pricing.py ─► policies.py ─► QR + certificate ─► /verify/policy/{token}
```

**Screen.** Rules live in `config/rules.yaml`, each with a weight, a condition and the sentence staff see. Change a weight or threshold there and rerun `make train`; no code changes. The IsolationForest looks at density, value per kg, lead time, shipper age and route frequency. It only adds a few points for the most unusual 10% of bookings, and a booking with no rule hit is capped at 39, so the model is never the only reason anyone is flagged. Scoring runs in two passes: first each booking on its own evidence, then again once the engine knows which shippers are flagged, so links to them count.

Bands: LOW 0 to 39, MEDIUM 40 to 69, HIGH 70+. HIGH goes to the review queue.

On the seeded data the engine catches all 120 planted risks and flags about 0.1% of clean bookings as MEDIUM or higher (`make report` prints the numbers; it grades only the seeded bookings, since live-feed bookings have no known answer). Model points below 3 are dropped, so the reasons on screen always add up to the score, apart from the cap at 100.

HIGH bookings whose flights left before the seed ran come with a past reviewer decision (approved or rejected, with a note), so the live review queue only holds flights still to go.

**Fraud graph.** Shippers are linked through shared phone numbers, emails and payer account references (`contact_link` table). `GET /graph/shipper/{id}` returns everyone within 2 hops. A shipper is *flagged* if KYC failed, a reviewer rejected one of their bookings, or one of their bookings scores HIGH on its own evidence. Sharing an agent's phone number with other clean shippers is not, on its own, a reason to flag anyone; the demo includes such groups.

**Cover.** `cargoshield/pricing.py`, settings in `config/settings.yaml`:

```
carrier liability = weight × 26 SDR/kg × SDR-to-USD rate     (Montreal Convention, Art. 22(3))
sum insured       = declared value − carrier liability
expected loss     = sum insured × base loss rate (commodity) × risk multiplier (band)
gross premium     = expected loss ÷ (1 − expense loading − profit margin)
premium           = max(gross premium, minimum premium)
```

Money is rounded half-up to the cent at each line, and each line is worked from the line above as shown, so every step can be redone on a calculator; the profit line takes the rounding cent. Cover is only offered before the flight departs, and not when the top-up is under $1,000 (`minimum_sum_insured_usd`), where the $25 minimum premium would be a silly rate.

The 26 SDR/kg limit has applied since 28 December 2024 (up from 22). It applies to the actual weight of the packages (Art. 22(4)). Verify it, and the SDR rate, before any real use. Issuing cover creates a random 192-bit token and a QR code that links to `/verify/policy/{token}`.

**Verify.** `GET /verify/awb/{awb}` checks the check digit, then looks the number up. `POST /verify/image` reads any QR code, then OCRs the printed text and cross-checks the two: a QR must point to the CargoShield host (no look-alike sites, no missing scheme, no `user@host` tricks), and a genuine policy QR on a certificate printed with a different AWB comes back SUSPICIOUS. If the photo can't be read it asks for the number (OCR gives up after 12 seconds). `GET /verify/policy/{token}` confirms cover is real and active. Policy numbers are sequential, so checking cover by number needs the AWB too.

Answers carry route, flight date, pieces and weight only: never shipper names or contact details. A booking waiting for a reviewer reads "Booked", exactly like any other, so the internal flag never shows. Only a booking a reviewer rejected reads differently (NOT ACTIVE, with a warning not to pay), which is what the airline would tell anyone who asked. Every check goes into `verification_log`; anything that isn't an AWB is logged as a digit count, never the raw text, so phone numbers people type don't end up in the log. Opened in a browser, the verify links show a phone-friendly page; called from code, they return JSON.

**WhatsApp.** The demo uses a chat simulator in the UI. All message handling sits behind one function, `messaging.handle_message()`. `cargoshield/whatsapp.py` shows the adapter for the WhatsApp Business Cloud API (parse the webhook, build the reply). It is not connected and has not been tested against the live service.

## Project layout

```
cargo-shield/
  config/           settings.yaml, rules.yaml, commodities.yaml
  data/             routes.csv, fakes/ (generated), database and model (generated, git-ignored)
  cargoshield/      the app: seed, risk, anomaly, graph, pricing, policies, verify, messaging, app
    static/         dashboard (plain HTML, CSS, JS; vis-network bundled for offline use)
    schema.sql      SQLite schema; schema_postgres.sql is the same tables for Postgres
  tests/            pytest suite, including the 3-minute script end to end
  DEMO.md           the click path for the presentation
```

## Changes from the brief, and why

- **Folder, not a separate repo.** The code lives in `cargo-shield/` inside this repository because this workspace can only push here. `git subtree split --prefix cargo-shield` moves it into its own `cargo-shield` repo with history whenever you want.
- **Three extra tables**: `review_decision` (approve/hold/reject with a note), `seed_truth` (which planted pattern each risky booking carries; only the tests read it) and `demo_meta`. Bookings also have a `payment_ref` column, because the fraud graph needs payer references and the brief's booking table had nowhere to keep them.
- **vis-network is bundled** in `cargoshield/static/vendor/` instead of loaded from a CDN, because the demo must work offline.
- **SUSPICIOUS vs INVALID FORMAT.** A typed number that fails the check digit is INVALID FORMAT (probably a typo). The same number printed on a document is SUSPICIOUS, because the airline could never have issued it.
- **NOT ACTIVE** is an extra verify result for a booking a reviewer rejected (and for cancelled cover), so a real AWB that will never fly doesn't get a green tick.

## Known limits (say these out loud if asked)

- **Top-up cover leaves a gap.** It pays the difference between declared value and the carrier's liability limit. But the carrier isn't liable for everything (Article 18 defences such as inherent defect, defective packing by someone else, war, or acts of public authority). An all-risks cargo policy that covers full value and recovers from the carrier is the usual design. Worth deciding before a real product.
- **Prices are placeholders.** Base loss rates, loadings and multipliers are made up. A real rate needs the airline's claims history.
- **Public verify endpoints can be scraped.** Anyone can try AWB numbers (the check digit cuts guessing by 7x, no more). A pilot needs rate limiting and probably a lighter answer (e.g. route only) for unauthenticated callers.
- **The dashboard has no login.** It is locked to the presenter's laptop, which is fine for a demo and nothing more.
- **OCR on real phone photos** is weaker than on these clean renders. Glare, folds and handwriting will cause misses; the chat falls back to asking for the number.
- **Times** are stored as UTC and shown in the browser's local time; route departure times are placeholders. If the schedule has no flight in the next 11 hours when you seed, the HIGH demo booking gets a departure 6 hours out on a route that flies that day, which may not match the timetable.
- **Small seeds.** Below about 1,000 bookings the planted share runs a little over 5%, so every pattern still appears at least twice.
- **SQLite** is fine for a demo. The schema is written to move to Postgres; the queries use plain SQL.

## Data protection notes

- Kenya's Data Protection Act, 2019 applies as soon as real shipper data is used.
- The ODPC's Guidance Note on Cross-border Data Transfers (consulted on in April and May 2026, final version published in September 2026) treats processing personal data in a cloud environment with servers outside Kenya as a cross-border transfer. Backups abroad count too. A real pilot should host in Kenya, or document the legal basis, risk assessment and safeguards for each transfer. Read the guidance note itself before relying on this summary.
- Verification responses never expose shipper personal data. The dashboard masks phone numbers, emails and payer references even for staff.
- Flags are internal. Nothing in the system publicly labels anyone a fraudster, and no booking is rejected without a person deciding.
