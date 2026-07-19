# Operator checklist (Devpost submission is operator-only)

Deadline: 2026-08-10 5:00pm ET (the live Devpost countdown is the binding
clock). Target submit: on or before 2026-08-08. Judging window: Aug 17-31.

## Side-deadlines FIRST (check these before anything else)

- [ ] Re-read the Devpost site for any side-deadline that closes before the
      submission deadline (credit/perk forms, feedback survey opt-in, team
      registration confirmations). These often close days early.
- [ ] Confirm registration status on datahub.devpost.com for the submitting
      account.

## Upload steps (agent-automatable except the final Devpost form)

1. [ ] YouTube upload: `demo/out/final.mp4` with title/description/tags/
       chapters from `docs/submission/YOUTUBE.md` (re-time chapters against
       the final cut first). PUBLIC, not made for kids. Verify logged-out
       via the oembed endpoint (title match).
2. [ ] Paste the YouTube URL into `docs/submission/DEVPOST.md` (replacing
       VIDEO_URL_PLACEHOLDER) and into the Devpost "Video demo link".
3. [ ] Devpost Create project (OPERATOR ONLY, reCAPTCHA): paste from
       `docs/submission/DEVPOST.md`; upload `docs/thumbnail.png`; upload
       `docs/screenshots/01..06.png` in order with captions from
       `docs/submission/SCREENSHOT_CAPTIONS.md`; attach the PDF brief
       (`docs/Notary - Judges' Technical Brief.pdf`) if the form allows.
4. [ ] Sweep the actual submission form for vendor-specific evidence fields
       (session IDs, attestations) before deadline day.

## Pre-submit verification (run the day of submission)

- [ ] https://notary-replay.vercel.app returns 200 logged-out and carries
      the title "Notary: replay of a captured run".
- [ ] Repo README quickstart still works against a fresh DataHub quickstart
      (or at minimum `python -m notary.eval` on a clean clone).
- [ ] Freeze SHA recorded below matches origin/main HEAD.

## Freeze

- Freeze SHA: FILL AT FREEZE (origin/main after the demo-and-submission PR
  merges; verify local == origin).
- After freeze: at most ONE late slice on the weakest judged axis, additive
  only, kill-dated; the recorded video must stay valid without re-render.
  The only always-permitted edit is the video-URL placeholder.

## Care window (through judging, Aug 17-31)

- [ ] Keep notary-replay.vercel.app and the repo public and unchanged.
- [ ] Never edit the Devpost submission after the deadline.
- [ ] Watch the registered email (dan.mercede@orionintelligenceagency.com):
      winner verification can require a reply within ~2 business days.
- [ ] Teardown (if any) only AFTER results are announced.

## Weakest judged axis (named at pick time)

Innovation/differentiation risk was named the weakest axis: verification
agents exist as a category. The counter is the honest-evaluation story and
the write-back-where-agents-look loop; the single post-freeze late slice, if
any, should strengthen exactly that axis.
