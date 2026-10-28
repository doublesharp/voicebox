# Voicebox DS implementation status

Local Coordinate is the authoritative task and evidence record. Project: `project_14a676bd-5cff-4ffe-ad9c-9aa08abc554e`; effort: `effort_efc6c236-4604-4cb2-95ee-f1e66c8e6320`. This is a short working view of the 2026-10-28 implementation.

| Feature | Implementation state |
| --- | --- |
| DoubleSharp identity and theme | Implemented; supplied logo, edition attribution, splash/settings/sidebar and display titles |
| Projects, scripts and cast | Implemented; owned SQLite/audio, ordered passages and frozen voice/model variants |
| Database and owned audio | Implemented; numbered transactional upgrades, verified pre-upgrade backups, newer-schema refusal, shared staged/hash-verified publication and quarantined orphan recovery |
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
- Complete frozen capability/job provenance, archive scope and missing optional-reference handling, plus atomic native destination publication.
- Remaining replacement-trim and Story-to-Studio workflow affordances identified in the S1 gap audit.
- Applicable integration and runtime acceptance in the spec, with unrun environments tracked separately from missing feature code.

Verification for the integrated testing branch: 274 backend checks, 181 frontend checks across 19 isolated test files, and seven device-independent native PCM/control tests pass. App and web no-emit typechecks pass. Independent scoped source reviews accepted the shared inference owner and playback handoff repairs. Frontend files run in separate processes because existing module mocks contaminate a combined invocation.

The existing complete Scratch Python environment supplies the compatibility dependencies. Full native Cargo compilation remains unverified: the offline cache lacks `symphonia`. A standalone Tauri TypeScript check also reports window/virtual-module declarations and a missing workspace dependency link; it is not a passing gate. The original checkout's uncommitted playback helper and four failing fixtures are preserved outside these merges.

A real offline renderer check against the final renderer/timing source used a 30-minute source split into 200 clips and produced the expected 48 kHz stereo WAV24 sample count. It completed in about 72 seconds with about 195 MiB process peak RSS. This measures that renderer fixture, excluding model inference, browser memory, UI timing and native audio. Raw evidence stays on Scratch and is referenced in Coordinate.

Open qualification includes real preset/cloned/directed engines, GPU cancellation/contention, packaged lifecycle and output routing, listening and independent metering, rendered 800×600/200% zoom/accessibility checks, and native destination publication. Later S2 video automation, S3 collaboration and S4 calling/advanced capabilities remain separate phases.


## Testing branch integration

All 15 local feature, fix, improvement and test branch tips are included in the integrated history. This includes the audio-quality aggregate, TTS pipeline, native playback sessions, Story scheduling, pause preservation, clean reference capture and offline quality tools. Historical upstream branches are outside this integration.

Merge repairs retain one shared TTS lifecycle owner, queue pending native starts before Studio handover, preserve output-clock/version-edit safeguards, and consolidate the earlier reference preference into microphone settings. Reference recording retains the recorder's actual fallback container type. This integration makes the accumulated work available for testing; the S1 feature and runtime gaps above remain open.

Persistence integration: Story/archive imports now use the same verified staged publisher as uploads. Real file/SQLite fixtures cover interrupted and corrupted copies, later-asset registration rollback, uncertain successful commits, migration failure, backup preservation and concurrent recovery/publication. Independent source reviews accepted the migration, publication and schema ownership changes; native/cross-platform qualification remains separate.
