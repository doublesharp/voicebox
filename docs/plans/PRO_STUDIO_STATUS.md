# Voicebox DS implementation status

Local Coordinate is the authoritative task and evidence record. Project: `project_14a676bd-5cff-4ffe-ad9c-9aa08abc554e`; effort: `effort_efc6c236-4604-4cb2-95ee-f1e66c8e6320`. This is a short working view of the 2026-10-28 implementation.

| Feature | Implementation state |
| --- | --- |
| DoubleSharp identity and theme | Implemented; supplied logo, edition attribution, splash/settings/sidebar and display titles |
| Projects, scripts and cast | Implemented; owned SQLite/audio, ordered passages and frozen voice/model variants |
| Database and owned audio | Implemented; numbered transactional upgrades, verified pre-upgrade backups, newer-schema refusal, shared staged/hash-verified publication and quarantined orphan recovery |
| Recovery and history | Implemented; revision checks, operation identity, independent window journals, undo/redo and immutable checkpoints |
| Generation | Implemented; per-passage capability checks, frozen known adapter/runtime identity, durable phase/attempt history, commercial-only policy, retry identity, cancellation, stale-adoption protection and shared model ownership |
| Retained takes and comparison | Implemented; undoable names/notes with durable recovery, temporary sampled-level A/B attenuation, frozen snapshots and explicit short-take replacement choices |
| Arrangement | Implemented; selected-take and raw music/SFX insertion, voice tracks, sample placement, trims, split, gain, fades and mute/solo |
| Review and delivery | Persistent checkpoint notes/checklists and local approval are merged; narration coverage, earlier-recording acknowledgement, mix/dry stems, WAV/MP3 and actual-file audition remain available |
| Archives and imports | Implemented; copied Story/audio ownership, validated portable graph, hashes and clean-profile reopening |
| Playback and visuals | Implemented; bounded preview ranges, awaited native ownership, cached waveforms, live sample-peak meters and reduced-motion styling |

The testing branch is `doublesharp` at `aedb3d5`. Persistent reviews and native staged publication are merged. Root is reviewing the remaining mixing and Story-copy changes separately; uncommitted implementation is not included in that testing revision.

## Remaining S1 implementation

The current core is a foundation slice, not full S1 acceptance. The user has requested that these gaps be completed in feature branches and merged into `doublesharp` as each slice passes its checks:

- Mixing/mastering: track effects, master gain, oversampled limiting, dry/processed output and stems are implemented. Independent review found uncertain-commit cleanup and unsupported pitch-processing faults; root repaired both and a fresh scoped recheck is pending. Legacy Effects remains available.
- Persistent revision-bound notes/checklists are merged with durable recovery and frozen local approvals. The exact-replay field-order defect was repaired and independently rechecked.
- Explicit archive scope and missing optional-regeneration-reference handling are being integrated. Native staged destination publication is merged; dialogs, Windows replacement/ACL behavior and packaged runtime remain unqualified.
- Story Create studio copy, durable retry identity and an accessible empty-arrangement import target are implemented. A copied-Story stem identity repair is undergoing independent recheck.
- Explicit recoverable storage cleanup with a complete retained-reference preview (P4.7) is being implemented. Automatic deletion remains disabled.
- Applicable integration and runtime acceptance in the spec, with unrun environments tracked separately from missing feature code.

Verification for the integrated testing branch: 317 backend checks, 223 frontend checks across 26 isolated test files, and seven device-independent native PCM/control tests pass. App and web no-emit typechecks pass. Independent scoped source reviews accepted shared inference/playback ownership and the capability/take integration repairs. The web production build also passes. Frontend files run in separate processes because existing module mocks contaminate a combined invocation.

The existing complete Scratch Python environment supplies the compatibility dependencies. The native macOS source/test bridge compiles with `cargo check --locked --offline --release --tests` using the existing Scratch cache. Existing real sidecars satisfy compile-time resource presence; this does not qualify their freshness or packaged launch. A standalone Tauri TypeScript check also reports window/virtual-module declarations and a missing workspace dependency link; it is not a passing gate. The original checkout's uncommitted playback helper and four failing fixtures are preserved outside these merges.

A real offline renderer check against the final renderer/timing source used a 30-minute source split into 200 clips and produced the expected 48 kHz stereo WAV24 sample count. It completed in about 72 seconds with about 195 MiB process peak RSS. This measures that renderer fixture, excluding model inference, browser memory, UI timing and native audio. Raw evidence stays on Scratch and is referenced in Coordinate.

Open qualification includes real preset/cloned/directed engines, GPU cancellation/contention, packaged lifecycle and output routing, listening and independent metering, rendered 800×600/200% zoom/accessibility checks, and native destination publication. Later S2 video automation, S3 collaboration and S4 calling/advanced capabilities remain separate phases.


## Testing branch integration

Previously committed feature, fix, improvement and test branch tips are included in the integrated history; the remaining uncommitted S1 slices are tracked above. This includes the audio-quality aggregate, TTS pipeline, native playback sessions, Story scheduling, pause preservation, clean reference capture and offline quality tools. Historical upstream branches are outside this integration.

Merge repairs retain one shared TTS lifecycle owner, queue pending native starts before Studio handover, preserve output-clock/version-edit safeguards, and consolidate the earlier reference preference into microphone settings. Reference recording retains the recorder's actual fallback container type. This integration makes the accumulated work available for testing; the S1 feature and runtime gaps above remain open.

Persistence integration: Story/archive imports now use the same verified staged publisher as uploads. Real file/SQLite fixtures cover interrupted and corrupted copies, later-asset registration rollback, uncertain successful commits, migration failure, backup preservation and concurrent recovery/publication. Independent source reviews accepted the migration, publication and schema ownership changes; native/cross-platform qualification remains separate.

Direction/take integration: registry/runtime identity now follows the actual engine adapter and relevant package versions; unresolved model terms stay excluded from commercial-only generation. Inactive direction is preserved, ordinary bracketed prose is not treated as an emotion token, and project polling exposes generation phases. Both new review findings (runtime identity and unjournaled take notes) were reproduced and repaired with scoped independent rechecks. Annotation keystrokes use journal version 2 with legacy recovery support. Sampled A/B levels are listening estimates, not integrated loudness or true-peak qualification.
