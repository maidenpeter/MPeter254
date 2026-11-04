# The 3-minute demo

## Before you walk in

1. On the day, run `make demo`. It reseeds, so the HIGH booking sits on a flight that hasn't left yet, whatever time you run it.
2. Open http://localhost:8000 in a full-screen browser window.
3. Run `make doctor` once. If Tesseract is missing, step 1 still works: the chat will ask for the number, and you type `999-47182030`.
4. Rehearsing? Hit **Reset** (top right) or run `make reset` between runs. It puts everything back exactly as it was after `make demo`.
5. Want to scan the QR with a real phone? Start with `make demo HOST=0.0.0.0`, open the dashboard on the LAN address it prints, and keep the phone on the same Wi-Fi. The phone can open the verification page and nothing else; the dashboard only answers your laptop. Otherwise use the on-screen "Scan the QR" button, which opens exactly what a phone camera would.

Click **Demo path** (top right) for a small presenter card with the three steps. After the first click it shrinks to three numbered buttons in the bottom-left corner.

The AWB numbers below are fixed by the seed, so they are the same on every laptop.

## The click path

### Opening line (15 seconds)

"Every number on this screen is synthetic, built on the public schedule. CargoShield does three things off one engine: it tells anyone whether paperwork is real, it scores every booking before you accept it, and it sells cover priced from that same score."

Point at the top strip: bookings screened, flagged for review, policies, premium, fakes caught.

### Step 1: a fake air waybill (45 seconds)

1. Demo path, step **1**. The Verify tab opens with one document highlighted: a fake air waybill for 340 kg of mobile phones, NBO to Lagos, stamped "Cleared for release" with a note demanding a USD 1,850 clearing fee.
2. Click the highlighted document. It goes into the chat as a photo.
3. In a couple of seconds: **NOT FOUND**. "Demo Airline has no booking with air waybill 999-47182030. Do not pay anyone, release goods or hand over money on the strength of this document."

Say: "A buyer in Lagos gets this on WhatsApp and is asked to pay a clearing fee. One photo, one answer, before the money moves. The Fakes caught counter just went up by one."

Optional: click the fake with the look-alike QR code. It comes back SUSPICIOUS because the QR points to a site that isn't ours.

### Step 2: a HIGH booking and the ring behind it (60 seconds)

1. Demo path, step **2**. Booking **999-60422180** opens: 6 kg of gold bars, $640,000 declared, score **100, HIGH**.
2. Read the reasons, top to bottom:
   - New shipper, 12 days old, booking high-value goods
   - Shares a phone number with a flagged shipper
   - Booked a few hours before departure and paid in cash
   - KYC not complete
   - The model's note, marked as a supporting signal only
3. Scroll to **Connections**. Five shippers light up, two of them red (this one and one other), joined by a shared phone, a shared email and a shared payer account.

Say: "Each sentence is a rule your team can read and tune. The model only adds weight; it never flags anyone on its own. And this booking isn't rejected. It's waiting for a person in the review queue, before the plane leaves." (Point at the Review queue tab count.)

If asked what the public sees: verify this AWB in the chat. It says the AWB is genuine and booked, nothing about review. The flag stays internal.

### Step 3: a clean booking, cover, and the QR (60 seconds)

1. Demo path, step **3**. Booking **999-31509461** opens: 420 kg of pharmaceuticals, NBO to Lusaka, $68,500 declared, score **0, LOW**. No connections.
2. Scroll to **Cover**. Walk the quote: carrier liability (420 kg × 26 SDR × 1.36), sum insured $53,648.80, expected loss, loadings, premium **$330.15**. "Illustrative pricing; an actuary can follow every line."
3. Click **Issue cover**. A policy number and a QR code appear, and a message confirms the new Policies and Premium totals (the counters behind the drawer go up too).
4. Click **Scan the QR**. A phone-sized page opens: **GENUINE**, cover active, AWB, route and sum insured. No shipper details.

Optional finish: click **Send the certificate to the chat**. The chat reads the QR off the certificate and answers GENUINE.

### Close (15 seconds)

"Same engine, three revenue lines: fewer scams in your name, fewer bad bookings on your aircraft, and premium on the good ones. Next step is a pilot on real schedule data, hosted in Kenya."

## If something goes wrong

| Problem | Fix |
|---|---|
| Page shows "Couldn't reach the demo database" | Run `make seed`, then reload. |
| The fake photo comes back "please type the number" | Tesseract isn't installed, or OCR took longer than 12 seconds and gave up. Type `999-47182030` into the chat. |
| Numbers look different from rehearsal | Click Reset (top right). |
| The HIGH booking's flight date is in the past | You seeded on an earlier day and used Reset since. Run `make demo` again. |
| A phone on the Wi-Fi gets "Staff pages are only available on the presenter's laptop" | Expected: only the verify page is public. Show the dashboard from the laptop. |
| Port 8000 is busy | `make demo PORT=8010` |

## Extra moments, if there's time

- Tick **Live feed** on the Bookings tab. A new booking arrives every few seconds and gets scored on the spot; about one in eight carries a risk pattern.
- Open the **Review queue**. The banner shows the engine check: every planted risk caught, and the false-positive rate on clean bookings. Approve, hold or reject a booking (rejecting needs a note).
- Search the bookings for `General goods` and filter to Flagged to show the vague-description rule.
