# Voicebox DS implementation status

S1 feature implementation is complete for testing. Full runtime acceptance remains open.

The testing branch is `doublesharp`. Verified code merge: `c79bbeda6611df534cb6322725ea87aa7fbb9b19`; Subsequent changes add desktop TypeScript declaration/alias configuration and remove one obsolete suppression; application runtime behavior is unchanged. Desktop types and the desktop UI build pass independently.

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

The exact clean code merge passed **386 backend tests**, **244 frontend checks in 31 isolated test files**, app/web no-emit typechecks and the web production build. The desktop TypeScript target and desktop UI build also pass after aligning its configuration with the existing shared declarations and Vite alias. Frontend files run in separate processes because existing mocks contaminate combined runs. Existing compatible Python and dependency caches were reused; no models or packages were installed for these checks.

The native macOS source/test bridge passes `cargo check --locked --offline --release --tests` at the earlier native/review merge; native Rust source is unchanged in the final code merge. Existing real sidecars satisfied compile-time resource presence, which does not qualify their freshness or packaged launch. Pure filesystem fixtures cover replacement, stream/rename failure and private Unix permissions; simulated write/flush/sync errors are not actual OS fault injection.

Independent source reviews accepted the integrated inference/playback ownership and the completed feature slices. Reproduced repairs include recovery identity, review replay key order, uncertain render publication, unsupported pitch processing, imported Story stem identity, disabled slider gestures, portable track recipe preservation and malformed frozen archive snapshots. Logs, exact hashes, review reports and command receipts were retained outside Git in task-owned run reports.

An earlier renderer baseline produced a 30-minute/200-clip WAV fixture in about 72 seconds with about 195 MiB process peak RSS. That receipt predates the new effects pipeline and is not a current application performance qualification.

## Local visual testing

Terra Max navigated the current source UI against an isolated local backend at 1440×900 and 800×600, plus a 720×450 viewport approximation. Available sidebar screens, a UI-created two-passage Studio project, Edit/Mix/Review, a checkpoint, Deliver's missing-audio preflight and an Effects keyboard selection were checked. The pass repaired wrapped Story composer controls, long-title overlap and access to Generate's empty-history state on short screens.

Root also repaired native-fetch receiver errors across Studio, review, storage and Story copying, and a deletion race that could reopen a deleted Story from cached choices. The browser test workaround was removed; the unmodified browser API loads Studio, and a UI-created Story deletes without stale detail or a reload. Focused regressions cover delayed list results, failed deletion and selection changes while deletion is pending. The affected frontend checks, app/web types and production web build pass; bounded evidence was retained outside Git.

This pass used available empty and script-populated states. The 720×450 check approximates reduced workspace area; actual 200% browser zoom was not established by the preview shortcuts. Populated audio, full accessibility, real models and packaged desktop behavior remain separate gates.

## Local speech quality continuation

Phrase repairs now expose explicit cached-Whisper take/script checks. Saved
reports flag possible omissions, substitutions, repetitions and extra words;
users can audition the interval and prepare editable phrase-repair controls.
This never rewrites the script, generates a replacement or adopts a take on its
own. Audio/request/script provenance and revision checks reject stale results;
portable archives retain report identity.

Captions can use saved recognized word times to create an unreviewed draft.
These are recognized-word timings, not supplied-text forced alignment or proof
that every script word was spoken. The existing wording/timing review gate
still applies. A real cached Whisper run produced word times from synthetic
speech; component/API/archive checks cover application behavior. Rendered
quality journeys, listening and packaged recognition remain separate gates.
Reference Quality also ranks short level-based intervals for audition while
preserving the original recording.

## Qualification still open

- Wider preset/cloned/directed model and GPU-contention qualification beyond the
  bounded real local-AI runs documented in the implementation report.
- Populated-audio browser layouts, full keyboard/accessibility and recovery/navigation journeys, and actual 200% browser zoom beyond the bounded visual pass above.
- Packaged lifecycle, native output routing/save dialogs, HTTP failure journeys and Windows replacement/ACL behavior.
- Listening, independent metering, final encoded true-peak/loudness compliance and long-project UI/effects performance.
- Actual disk-full, power-loss and OS write/flush failure behavior; current failure fixtures are bounded/injected.

The earlier desktop TypeScript declaration and resolution errors were repaired without installing dependencies. Unrelated work and packaging outputs were preserved during integration; later playback repairs are recorded in the upstream integration report.

S2 video automation, S3 collaboration and S4 calling/advanced capabilities remain later phases. No release, signing, notarization, deployment or updater-channel change is claimed by this testing integration.
