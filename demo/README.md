# Demo assets

These files are the recording provenance of the submitted demo video, not a
portable render target: `demo.config.json` and the `file://` card URL in
`DEMO_SCRIPT.md` deliberately pin the absolute workstation paths the video
was rendered from, matching the estate's other hackathon demo configs. To
re-render elsewhere, update those two paths to your checkout.

- `DEMO_SCRIPT.md`: shots, narration, and browser actions (the narration
  lines are the spoken claims; they went through the adversarial overclaim
  review recorded in `docs/submission/REVIEW_VERDICT.md`).
- `cards/run-cli.html`: the terminal card; its lines are the verbatim stdout
  of the recorded run, receipt at `captures/run-cli-2026-07-18.txt`.
- `cards/thumbnail.html`: source of `docs/thumbnail.png`.
- `out/` (gitignored): rendered artifacts; the final MP4 uploads to YouTube
  per `docs/submission/OPERATOR_CHECKLIST.md`.
