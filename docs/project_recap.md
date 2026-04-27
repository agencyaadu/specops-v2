# SPEC-OPS v2 — What we built today

**Date:** 27 April 2026

---

## In one line

A complete rebuild of SPEC-OPS where the team works inside Discord and
manages their profile on a website. Both connect to the same database,
so what happens in Discord shows up on the web instantly.

---

## What's working today

### Onboarding (live)
- New people type **`/onboard`** in the Discord `#onboarding` channel.
- A small form appears asking for **PAN, full name, and WhatsApp number**.
- After submitting, a **Sign in with Google** button appears — one click
  links their Google account so they can log in to the website later.
- Every onboarded person ends up with a profile in the system.

### Web profile (live)
URL: **https://specops-v2.up.railway.app**

- Sign in with **PAN + Google** (the same Google they linked in Discord).
- See a personal **home page** with their name, joining date, active
  operations they're assigned to, and total stats.
- Open the **profile page** to view or edit personal details: date of
  birth, location, languages, headshot link, intro video link, bank,
  account number, IFSC, UPI ID.

### Background sync (live)
- Selected database tables (people + attendance) automatically copy
  themselves to a Google Sheet every 5 minutes — no extra work needed.

---

## How a team member uses it

1. Open Discord → `#onboarding` channel.
2. Type `/onboard` → fill the form (PAN, name, WhatsApp) → submit.
3. Tap **Sign in with Google** → pick their Google account → done.
4. Open https://specops-v2.up.railway.app
5. Enter their PAN → click **Continue with Google** → land on home page.
6. Tap **Edit profile** → fill the rest (DOB, bank, headshot, etc.) → save.

**Total time:** about 2 minutes.

---

## Numbers so far

- **24 factories** loaded from the old system, cleaned up.
- **29 operations** loaded, each given a short readable ID like
  `MU-AI-U1-NI` (Mumbai · Antariksh Infra · Unit 1 · Night).
- **31 chief / captain assignments** queued — they activate as each of
  those people completes onboarding.
- **1 person** onboarded so far (the test account).

---

## What's coming next (Batch 2)

Once everyone is onboarded:

1. **Attendance** — operators clock in to their op with a photo. The
   captain assigned to that op approves or rejects in Discord.
2. **Drag-and-drop assignments** on the website — visually move people
   between ops and roles instead of typing commands.
3. **Activity heatmap** on each profile — small grid showing the last
   7-14 days of attendance for the active op, plus a list of past ops.
4. **Validation reminders** — the bot DMs the assigned captain if a
   request sits unconfirmed for too long.
5. **Admin views** — a single screen showing today's attendance across
   every op, plus a queue of pending validations.
6. **Factory + operation creation pages** on the web (currently we add
   them via scripts).

---

## What we need from the team

Two things to make tomorrow's batch run smoothly:

1. **Onboard now** — follow the 6 steps above. It takes 2 minutes.
2. **Keep your Google account** — the link between PAN ↔ Google ↔
   Discord is what lets you log in.

If anything trips you up, ping in the channel.

---

## Where things live

| Thing | Where |
|---|---|
| Web app | https://specops-v2.up.railway.app |
| Discord bot | **FREDDY** in the SPEC-OPS server |
| Database | Supabase (private) |
| Code | https://github.com/agencyaadu/specops-v2 |
| This document | `/recap.pdf` on the web app |

---

*See you in the next batch.*
