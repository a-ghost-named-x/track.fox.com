# How the numbers on track.fox.com work

A plain-language walkthrough of every number the site shows and where it comes from. Written for a review of the site, not for programmers — there is no code in here, just the rules. Where a rule came from a conversation with the floor, it says so.

The site has three pages that show numbers:

- **The boards** (`/dashboard/...`) — the big screens on the floor. Green and red cells, updated every couple of hours from the rounds.
- **Supervisor** (`/supervisor`) — the same grid, but for any past day and shift. For looking back.
- **OEE** (`/oee`) — one score per machine per shift, plus a chart of what caused the downtime.

Everything below feeds one of those three.

---

## The one thing to know before anything else

- Every target on this site comes from the standards spreadsheet the floor gave us.
- The floor told us those standards are set at **75% of what the machine can theoretically make**. So "hitting standard" means running at three-quarters of the machine's top speed. That's normal and intentional — nobody runs a machine flat-out for eight hours.
- To be clear: the numbers in the table (C1 = 11,700 per two hours) **already are** the 75% figure. The site never takes 75% of 11,700. The boards compare readings straight against 11,700. The only thing the site does with the 75% is go the other direction — 11,700 ÷ 0.75 = 15,600 — to work out the machine's top speed for OEE.
- That one fact explains the number people ask about most: **a machine that hits its standard exactly, with no downtime and no scrap, scores 75% OEE — not 100%.** Nothing is broken. 75% *is* the target. (More on this in the OEE section.)
- We have not independently verified the 75% figure — it's what we were told when the standards were handed over. If it's actually 80% or 70% for some machines, every OEE number shifts with it. It's on the list to confirm.

---

## How the readings work (the rounds)

- Every two hours someone walks the floor and writes down each machine's counter. That's the number typed into the site — at 8AM, 10AM, 12PM, 2PM, and so on through the day.
- **The number is a running total for the shift, not "what happened in the last two hours".** The counter starts at zero when the shift starts. So if C1 shows 11,700 at 8AM and 23,400 at 10AM, it made about 11,700 in each two-hour stretch.
- The counter resets at every shift change (6AM, 2PM, 10PM). Three shifts, four readings each, twelve readings a day.
- **A shift's production is simply its last reading.** Whatever the 2PM number says is what 1st Shift made. The site doesn't add anything up — the counter already did that.

### What a blank cell means

- The floor confirmed this on 2026-09-03: **a blank means "the number hasn't changed", not "nobody checked".**
- The crew leaves a checkpoint empty when there's nothing new to write — machine down, operator pulled to another line, waiting on a delivery. Usually the Issue column says why.
- So the site treats a blank as **zero made in that slot**. A blank first slot means the counter was still at zero.
- This matters more than it sounds. Blanks are a machine's *worst* hours — that's why they're blank. If the site ignored them, it would quietly drop exactly the bad hours and make every bad shift look better than it was. Real example: C2 scored 33.7% on a bad day when its blank was ignored, and 25.2% when it counted as zero. 25.2% was the truth.
- Two exceptions: if a machine has **no readings at all** for a shift, that's "nobody entered it" and it shows blank/N/A rather than zero. And hours that haven't happened yet are never counted as zero.

### Corrections

- Nothing is ever overwritten. If someone re-enters a number for the same machine and slot, the newest entry wins and the old one is kept underneath for the record.
- The boards and the Supervisor page both show the newest version. So Supervisor is "what we know now about that shift", not "what the screen said at 2PM".

---

## The boards — why a cell is green or red

- Each cell is one machine at one time slot. It shows the reading typed in on the rounds.
- The site looks up that machine's **target for that slot** in the standards table. If the reading is **at or above** the target, the cell is green. If it's below, red. That's the whole rule.
- Exactly on target counts as green.
- Because the readings are running totals, the targets are too. C1's targets for 1st Shift are 11,700 → 23,400 → 35,100 → 46,800. Same four numbers again for 2nd Shift and again for 3rd.
- Targets differ by machine. From the standards spreadsheet, per two-hour slot:

  | Machines | Per 2 hours | Per shift |
  |---|---|---|
  | C1–C5, C14–C16 | 11,700 | 46,800 |
  | C6–C11 | 10,800 | 43,200 |
  | WS1–WS6 | 9,900 | 39,600 |
  | FM1–FM3 | 8,100 | 32,400 |
  | P1 | 14,400 | 57,600 |
  | P2–P4 | 16,200 | 64,800 |
  | AS1–AS5 | 2,520 | 10,080 |
  | AS6–AS7 | 2,250 | 9,000 |

- The colour is decided the moment the number is saved, so if a target ever changes, old cells keep the colour they had at the time.
- A little white corner on a cell means an issue was logged against that slot. The Issue column lists them, tagged with the slot they were logged at.
- The board switches to the next shift one hour after the shift actually changes (so 3PM, not 2PM), to give the outgoing crew a moment to look at their final numbers. The entry form doesn't have that delay — you can keep entering 1st Shift numbers after 3PM.
- Machines with nothing entered yet are hidden from the board so the screen isn't full of empty rows. Supervisor does the opposite and shows every machine, because on that page an empty row *is* the finding.

---

## The OEE page

OEE stands for Overall Equipment Effectiveness. It's one score that answers: **out of everything this machine could have made this shift, how much good product actually came out?**

### The three ingredients

Each machine needs three things for one shift:

1. **Good units** — the shift's last reading from the rounds (see above).
2. **Downtime** — how many minutes it was stopped, and why. Entered once at the end of the shift on `/console/oee`. A machine that ran all shift with no stops still needs the "no downtime" box ticked, so the site knows someone actually checked.
3. **Scrap** — one total for the shift, same form.

Plus two settings:

- The **Scheduled** checkbox. Untick it for a machine that wasn't supposed to run (planned maintenance, no work for it). That machine is left out of the shift entirely — it doesn't score 0%, it just isn't in the picture.
- The **Shift length** — 8h, 10h or 12h — per machine, per day, *per shift*: set on the 1st Shift page for the 1st Shift and on the 2nd Shift page for the 2nd (a 3rd Shift is always 8h). A machine that ran 6AM to 6PM is judged against 720 minutes, not 480, and its shift is the six readings from 8AM to 6PM. The crew keeps writing the running count into the 4PM and 6PM boxes on the rounds exactly as they do now; only OEE reads them differently.
  - A shift's length says where it starts: the second 12-hour shift of the day is 6PM–6AM, the second 10-hour one is 4PM–2AM. So an 8-hour 1st Shift followed by a 12-hour crew at 6PM is just "2nd Shift: 12h" on the 2nd Shift page.
  - A shift with nothing set follows the one before it, so setting the 1st Shift to 12h makes the night a 12-hour 2nd Shift automatically.
  - The one rule: a later shift can be **as long or longer** than the one before it, never shorter — a shorter one would start inside it. The page greys out the options that would, and refuses them if posted anyway.
  - A 10h/12h 2nd Shift starts *unticked* (a night crew is the exception — picking the length on the 2nd Shift page ticks it; untick if no crew ran), and there is no 3rd Shift that night, so the 3rd Shift row for that machine says "no 3rd shift" instead of showing inputs.
  - The floor screens stay on the 8-hour rotation regardless — on a machine running long, the 2nd Shift board's colours don't mean anything, and the floor knows that.
- **The date is the day the shift started.** "3rd Shift, Thursday" on `/oee`, `/supervisor` and `/console/oee` is Thursday 10PM through Friday 6AM, and it's entered Friday morning under *Thursday's* date — the end-of-shift form defaults to yesterday when 3rd Shift is opened before noon and prints the span with both dates. The 2-hour rounds are the one place that still uses the morning's date for the 12AM–6AM readings; the review pages translate.

### The three factors

OEE is three percentages multiplied together:

- **Availability — "how much of the shift was it actually running?"**
  A shift is 480 minutes (600 or 720 if its length was set to 10h or 12h). Subtract the downtime minutes. 60 minutes down means it ran 420 of 480 = 87.5%.

- **Performance — "while it was running, how close to full speed was it?"**
  Take the machine's top speed (per minute), multiply by the minutes it ran — that's what it *could* have made. Divide what it actually made (good + scrap) by that.

- **Quality — "of what it made, how much was good?"**
  Good ÷ (good + scrap). 40,000 good and 500 scrap = 98.8%.

- **OEE = Availability × Performance × Quality.**

### Where "top speed" comes from

- Start from the number in the standards table: C1 = 11,700 per two hours. That figure was already discounted to 75% by the floor before it was handed to us — the site didn't do that and doesn't repeat it.
- To get back to what the machine can actually do, the site divides: 11,700 ÷ 0.75 = 15,600 per two hours = 7,800 an hour = 130 a minute. That's C1's top speed, and it's the only number OEE measures against.
- Each machine has its own. It's stored per machine so a single one can be corrected later if the 75% assumption turns out to be wrong for it.

### A worked example — C1, one shift

Say C1 finished the shift at 40,000 good, with 500 scrap and 60 minutes of downtime.

- Ran 420 of 480 minutes → **Availability 87.5%**
- Could have made 130 × 420 = 54,600 in that time; made 40,500 (good + scrap) → **Performance 74.2%**
- 40,000 good out of 40,500 → **Quality 98.8%**
- 87.5% × 74.2% × 98.8% → **OEE 64.1%**

Quick sanity check, and a handy way to think about it: OEE is also just **good units ÷ what the machine could have made if it ran flat-out all shift.** C1 flat-out for 480 minutes is 62,400. 40,000 ÷ 62,400 = 64.1%. Same answer — the downtime and scrap are already baked into the fact that only 40,000 came out.

That's why hitting standard = 75%: 46,800 ÷ 62,400 = 75%.

### The colours

- **85% and up** — excellent (generally considered world-class)
- **75% to 85%** — good (at or above standard)
- **60% to 75%** — fair (needs a look)
- **Under 60%** — poor
- **No colour** — nothing to score yet (see "when a machine shows nothing" below)

### The other columns

- **% of std** — good units ÷ the shift standard. The old-fashioned "did we hit target" number. C1 at 40,000 is 85% of its 46,800 standard. This is what the boards' green/red is based on; OEE is the stricter version of the same question.
- **Good** — the shift's last reading. Hover it to see all four checkpoints.
- **Scrap** — as entered.
- **Downtime** — total unplanned minutes as entered.
- **Top reasons** — the biggest downtime reasons for that machine. Hover for the full list and any note.

### The zone and floor totals

- The line at the top of each zone (and the floor total) is **not an average of the machines' percentages.** It adds up the actual units and minutes across every machine first, then does the division once. A machine that ran 20 minutes can't weigh the same as one that ran all shift.
- Machines that weren't scheduled, or that have no data yet, are left out — they don't drag the zone down.
- One wrinkle you don't need to worry about but might notice: for a *group* of machines, Availability is weighted by each machine's capacity, not just clock minutes. A minute of downtime on a Leno machine (1,680/hr) costs the floor a lot less than a minute on a Combo line (7,800/hr), and the rollup accounts for that. For a single machine it makes no difference.
- If some machines have scrap entered and some don't, OEE and Availability count all of them, but Performance and Quality only count the ones with scrap. The hover text says how many.

### The downtime chart (Pareto)

- Every downtime reason entered across the shift, added up by reason, biggest bar first, with each one's share of the total.
- The fourteen reasons are the floor's own list: Roll Change, Setup, FAAR, Equipment Failure, Operator Adjustments (split four ways — Temperature, Timing, Pressure, Air jet), Defective Material, Lack of Material, Lack of Operator, Delivery, Registration, Start of Shift. There's deliberately no "Other".
- Operator Adjustments was one reason until it was split (September 2026). Shifts entered before that keep the single "Operator Adjustments" bar — there's no way to know after the fact which kind each one was — so a date before the split shows one bar and a date after shows up to four. If you open an older shift on the entry form, that old reason is still listed on the machines that had it, tagged "retired", so a correction doesn't lose its minutes.
- A machine that has downtime entered but no production numbers still shows up here — 20 minutes waiting on material is a real 20 minutes.

---

## When a machine shows nothing, a dash, "N/A", "not scheduled", or "!"

The site's rule is **never guess.** A missing number is shown as missing, not filled in with a zero or a 100%. Hover the cell and it says why.

- **Blank OEE, production is there** — downtime hasn't been entered for that shift yet. The site won't assume "no downtime" because that would hand every un-entered machine a perfect Availability. Tick the "no downtime" box on `/console/oee` and the number appears.
- **Blank OEE, no Good number either** — nobody entered any readings for that shift.
- **OEE shows but Perf and Qual say N/A** — scrap wasn't entered. OEE doesn't actually need scrap (it cancels out in the multiplication), so the score is real. Scrap only splits Performance from Quality.
- **"not scheduled"** — someone unticked Scheduled for that machine and shift. Left out of the totals on purpose.
- **"!"** in red — the numbers can't be right and the machine is excluded from the totals until fixed. There are four causes, and the banner at the top of the page names the machine and the fix:
  - Last checkpoint is lower than an earlier one (the shift total would be wrong)
  - More than double the machine's top speed (usually a typo — a transposed digit, or a day's total typed into a shift's box)
  - More minutes of downtime than the shift has (480, 600 or 720)
  - No top speed set up for that machine (a setup issue, not a floor issue)
- **A small warning mark** on a score that still shows — worth a look but the machine still counts:
  - A mid-shift reading is lower than the one before it, but a later reading is higher, so the shift total is still right
  - The machine beat its calculated top speed. That's a good problem: either it really is faster than we think and its 75% figure is off, or its downtime was over-reported. The number is shown as-is, never capped at 100%.

---

## Things the site deliberately does not do

- **It doesn't judge a machine on one two-hour slot.** Readings aren't taken at exactly 8:00 and 10:00, so one slot can look like 19,500 and the next 0 just because someone walked the floor late. That's timing noise, not the machine. Everything is scored on the whole shift — 480 minutes (or 600/720) no matter when each reading was taken. (The site used to flag individual slots and got it wrong on seven machines on the first real day.)
- **It doesn't average percentages.** See the totals section.
- **It doesn't default anything.** No downtime entered ≠ no downtime. No scrap entered ≠ no scrap. Not entered is not entered.
- **It doesn't cap at 100%.** If a number comes out over, that's information.
- **It doesn't cross-check the manual counts against the machines' own counters.** Good + scrap is captured so someone can do that by hand if they want to; it's not an automatic check.

---

## Questions people usually ask

- **"C3 hit standard all shift and it's only showing 75%. What's wrong?"**
  Nothing. Standard is 75% of top speed by definition. 75% is the target. 85% is excellent.

- **"Why is this machine red on the board but its OEE looks okay?"**
  The board checks each two-hour slot; OEE looks at the whole shift. A slow first two hours and a fast last six gives red early cells and a fine OEE.

- **"Why does it say N/A when I know the machine ran fine?"**
  Someone needs to record that it ran fine — tick "no downtime" on `/console/oee`. The site won't assume it.

- **"Performance says 500%. That can't be right."**
  It isn't, and the site is telling you so. Performance over 100% means the machine made more than it physically could have in the run time recorded — so the downtime minutes and the production count contradict each other, and the downtime is almost always the wrong one. Real example: C1 on 9/10 had 420 minutes of downtime entered (7 of 8 hours) but produced 37,800 units, which needs at least 5 hours of running. Availability came out 12.5% and Performance 513%. Hover the Downtime cell to see what was entered and fix it on `/console/oee`. The OEE itself was still right (60.6%), because OEE doesn't depend on the downtime figure — only Availability and Performance were affected.

- **"Why is the zone number lower than most of the machines in it?"**
  Because it's not an average. One big machine having a bad shift moves the zone total more than three small ones having a great one.

- **"The 4AM reading was a typo and I fixed it. Does the old one still count?"**
  No — newest entry wins everywhere. The old one is kept in the history but doesn't show or score.

- **"Can I see what the board looked like at 2PM, before corrections?"**
  Not on the site today. The history is all there, so it could be built, but Supervisor shows the corrected picture.
