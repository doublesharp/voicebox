# Voicebox DS implementation status

Local Coordinate is the authoritative task and evidence record. Project: `project_14a676bd-5cff-4ffe-ad9c-9aa08abc554e`; effort: `effort_efc6c236-4604-4cb2-95ee-f1e66c8e6320`. This is a short working view of the 2026-10-28 implementation.

| Feature | Implementation state |
| --- | --- |
| DoubleSharp identity and theme | Implemented; supplied logo, edition attribution, splash/settings/sidebar and display titles |
| Projects, scripts and cast | Implemented; owned SQLite/audio, ordered passages and frozen voice/model variants |
| Recovery and history | Implemented; revision checks, operation identity, independent window journals, undo/redo and immutable checkpoints |
| Generation | Implemented; durable bounded jobs, explicit retry identity, cancellation, stale-adoption protection and shared model lifecycle ownership |
| Arrangement | Implemented; selected-take and raw music/SFX insertion, voice tracks, sample placement, trims, split, gain, fades and mute/solo |
| Review and delivery | Implemented; checkpoint review/approval, narration coverage, earlier-recording acknowledgement, mix/dry stems, WAV/MP3 and actual-file audition |
| Archives and imports | Implemented; copied Story/audio ownership, validated portable graph, hashes and clean-profile reopening |
| Playback and visuals | Implemented; bounded preview ranges, awaited native ownership, cached waveforms, live sample-peak meters and reduced-motion styling |

Implementation and review repairs are integrated. The final independent recheck accepted the other inspected corrections and reported two further P2s: cross-voice replacement routing and unsupported imported working formats. Both were reproduced, repaired, and verified by root with new regressions; the resulting patch has not received another independent review.

## Remaining S1 implementation

The current core is a foundation slice, not full S1 acceptance. The user has requested that these gaps be completed in feature branches and merged into `doublesharp` as each slice passes its checks:

- Take names and annotations, plus temporary level-matched A/B audition (P2.7–8).
- Studio effects, explicit master gain/limiting controls, dry/processed audition and processed voice stems (P5.3, P6.6). Legacy Effects remains accessible.
- A persistent revision-bound review checklist alongside existing local approval notes (P6.2).
- Applicable integration and runtime acceptance in the spec, with unrun environments tracked separately from missing feature code.

Verification: 113 backend/affected-legacy tests and 71 Studio frontend tests pass. App/web no-emit typechecks, web/Tauri frontend production builds, scoped Biome, new Python import lint/format checks and patch whitespace checks pass. Fixture tests, static checks and frontend builds do not establish native/model/browser acceptance. Current environment lacks `fastmcp` for the broader compatibility suite. The pre-existing `playback-output.test.ts` also has four failures against its pre-existing output helper; those unrelated edits are preserved.

A real offline renderer check against the final renderer/timing source used a 30-minute source split into 200 clips and produced the expected 48 kHz stereo WAV24 sample count. It completed in about 72 seconds with about 195 MiB process peak RSS. This measures that renderer fixture, excluding model inference, browser memory, UI timing and native audio. Raw evidence stays on Scratch and is referenced in Coordinate.

Open qualification includes real preset/cloned/directed engines, GPU cancellation/contention, packaged lifecycle and output routing, listening and independent metering, rendered 800×600/200% zoom/accessibility checks, and native destination publication. Later S2 video automation, S3 collaboration and S4 calling/advanced capabilities remain separate phases.
