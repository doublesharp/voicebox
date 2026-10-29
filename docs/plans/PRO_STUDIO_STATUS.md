# Voicebox DS implementation status

S1 feature implementation is complete for testing. Full runtime acceptance remains open. Local Coordinate is authoritative: project `project_14a676bd-5cff-4ffe-ad9c-9aa08abc554e`, effort `effort_efc6c236-4604-4cb2-95ee-f1e66c8e6320`, integration work `work_1e51884b-8640-4e14-9b21-a13b5a54efe1`.

The testing branch is `doublesharp`. Verified code merge: `c79bbeda6611df534cb6322725ea87aa7fbb9b19`; subsequent status-only commits do not change the tested application source.

| Feature | Implementation |
| --- | --- |
| DoubleSharp identity | Supplied logo, edition attribution, splash/settings/sidebar, display titles and restrained motion |
| Projects, scripts and cast | Owned SQLite/audio, ordered passages, frozen voice/model configuration and capability-aware direction |
| Persistence and recovery | Numbered migrations through version 5, verified backups, newer-schema refusal, staged audio publication, independent window journals, revision checks, undo/redo and checkpoints |
| Generation | Per-passage capability validation, commercial-only policy, durable request/attempt/phase history, retry/cancellation, adoption protection and one physical inference owner |
| Takes | Recoverable names/notes, retained alternatives, frozen provenance, temporary sampled-level A/B comparison and explicit short-take replacement choices |
| Arrangement and visuals | Raw audio and selected-take insertion, keyboard/drop import, voice routing, sample placement/trims/split/gain/fades/mute/solo, bounded preview, cached waveforms and live sample-peak meters |
| Mixing and mastering | Stateful bounded track effects, dry/processed output, aligned stems, master gain, editable loudness/peak targets and oversampled limiting; actual-file audition and frozen render provenance |
| Local review | Persistent checkpoint-specific notes/checklists, independent CAS/replay, durable recovery and frozen local approval snapshots |
| Delivery | Complete narration preflight, explicit earlier-recording consent, WAV24/MP3 preflight, render phases/attempts, preserved output through uncertain commits and truthful save receipts |
| Story copying | Create studio copy from Stories, durable retry identity, independently owned pinned audio, preserved placement/trims/gain/overlap and conservative pinned-source stem routing |
| Portable archives | Explicit all/used-take and reference inclusion scope, checkpoint/review preservation, validated graph/hashes/paths, missing optional-reference recovery and clean-profile reopening |
| Recoverable storage cleanup | Protected-reference preview, stale-preview checks, explicit selection/confirmation, verified trash publication, exact retry and restore |

Studio supports the existing effects that preserve block shape. Enabled pitch shifting is rejected with a legacy Effects alternative until its variable output can be aligned. Dry output bypasses effects and master processing; processed stems omit master normalization/limiting and preserve relative track levels. The oversampled limiter is not certified for final true-peak compliance after resampling or encoding. Unmeasurable audio stays unavailable.

Archives explicitly exclude model weights, disposable caches and undo history; recorded jobs are provenance only. Missing optional voice references preserve included playback and re-export, while regeneration requires voice reassignment. Required audio and present corrupt assets still fail validation. Track identity is rebound during omission/remapping so retained-take replacement and empty-track reuse preserve mix settings.

Cleanup retains audio reachable from drafts, checkpoints, history, takes, jobs, render inputs and retained metadata. It verifies a trash copy before committing source registration removal, and checks durable state before removing an original. Restore preserves recoverable bytes through uncertain acknowledgements. No automatic or permanent purge is offered: recoverable trash does not reduce total disk usage. Conservative historical receipts can retain otherwise unused audio.

## Verified source and fixtures

The exact clean code merge passed **386 backend tests**, **244 frontend checks in 31 isolated test files**, app/web no-emit typechecks and the web production build. Frontend files run in separate processes because existing mocks contaminate combined runs. The existing Scratch Python and dependency caches were reused; no models or packages were installed for these checks.

The native macOS source/test bridge passes `cargo check --locked --offline --release --tests` at the earlier native/review merge; native Rust source is unchanged in the final code merge. Existing real sidecars satisfied compile-time resource presence, which does not qualify their freshness or packaged launch. Pure filesystem fixtures cover replacement, stream/rename failure and private Unix permissions; simulated write/flush/sync errors are not actual OS fault injection.

Independent source reviews accepted the integrated inference/playback ownership and the completed feature slices. Reproduced repairs include recovery identity, review replay key order, uncertain render publication, unsupported pitch processing, imported Story stem identity, disabled slider gestures, portable track recipe preservation and malformed frozen archive snapshots. Logs, exact hashes, review reports and command receipts remain on Scratch and are referenced in Coordinate.

An earlier renderer baseline produced a 30-minute/200-clip WAV fixture in about 72 seconds with about 195 MiB process peak RSS. That receipt predates the new effects pipeline and is not a current application performance qualification.

## Qualification still open

- Real preset/cloned/directed models, GPU cancellation/contention and platform-specific model execution.
- Actual browser layout, keyboard/accessibility and recovery/navigation journeys at 800×600 and 200% zoom.
- Packaged lifecycle, native output routing/save dialogs, HTTP failure journeys and Windows replacement/ACL behavior.
- Listening, independent metering, final encoded true-peak/loudness compliance and long-project UI/effects performance.
- Actual disk-full, power-loss and OS write/flush failure behavior; current failure fixtures are bounded/injected.

A standalone Tauri TypeScript check previously reported existing window/virtual-module declarations and workspace dependency resolution errors; it is not a passing gate. The original checkout's three unrelated playback edits and four known failing playback-output fixtures remain preserved outside these merges. Other owners' worktrees and packaging outputs remain untouched.

S2 video automation, S3 collaboration and S4 calling/advanced capabilities remain later phases. No release, signing, notarization, deployment or updater-channel change is claimed by this testing integration.
