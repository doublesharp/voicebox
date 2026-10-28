# Voicebox DS: professional studio specification

**Status:** B0 branding implemented; S1 implementation authorized and in integration/review. Acceptance criteria remain open until verified. S2-S4 describe later phases.
**Date:** 2026-10-28
**Working identity:** Voicebox DS / DoubleSharp Edition
**Primary platform:** Local desktop application, retaining the existing web client.
**Implementation plan:** [PRO_STUDIO_PLAN.md](PRO_STUDIO_PLAN.md)

## 1. Product intent

Voicebox DS is a professional voice production workspace for creating, directing, revising, mixing, and delivering speech. A user should be able to reopen an older job, change one passage, preserve approved performances, and produce the required deliverables without reconstructing the project.

The first release serves solo creators and freelance editors producing narration, character dialogue, training material, and client voiceovers. Automated video production is the first integration use case. Client collaboration, localization, and reference-performance generation extend the same project model in later phases. Telephone outreach belongs to a separate future operations product surface.

### Intended outcomes

- Complete a job inside one project containing its script, cast, audio, timeline, revisions, and deliverables.
- Direct and compare performances using controls the selected engine actually supports.
- Reopen and play approved work without loading or downloading its generation models.
- Recover acknowledged edits after restart and preserve unfinished text when saving fails.
- Deliver audio whose timing and processing agree with the reviewed revision.
- Adopt selected upstream engine and compatibility improvements without requiring the studio workflow to track every upstream UI or schema change.

### Decision status

| Decision | Status and rationale |
| --- | --- |
| DoubleSharp edition identity | User requested implementation. Use Voicebox DS as the display name and DoubleSharp Edition as the edition label; retain upstream attribution. |
| Product-owned studio domain | Recommended. Projects, revisions, and asset ownership must remain coherent as upstream changes. |
| Independent roadmap with selective upstream adoption | Recommended strategy from the discussion; this document does not perform a repository or distribution cutover. |
| Local operation without an account | Proposed first-release default, consistent with the existing application. |
| Edit, Mix, Review workspaces; Deliver action | Proposed layout, combining the researched interaction patterns below. |
| Separate studio database and immutable asset storage | Proposed engineering boundary; avoids redefining upstream tables and cleanup rules. |
| Cloud services, hosted review, remote workers, new signing/update identity | Follow-on decisions with their own implementation and release gates. |

## 2. Existing foundations

Source inspection baseline: local commit `b7c79bea035c3a91a8e46a6322efc7ec7c9948e7`; cached upstream baseline `8af7efe62fab8d33e2a5dfbedabaa45f68a5184f`. These identify inspected source, not live runtime qualification. Concurrent playback changes in the checkout are outside this specification's implementation scope.

| Existing area | Reuse and limitation |
| --- | --- |
| [Global navigation](../../app/src/components/Sidebar.tsx) and [router](../../app/src/router.tsx) | Generate, Stories, Captures, Voices, Effects, Models, and Settings remain accessible during adoption. |
| [Story editor](../../app/src/components/StoriesTab/StoryTrackEditor.tsx) | Already supports timing, trimming, splitting, gain, regeneration, and take selection. Extend these concepts instead of describing them as new capabilities. |
| [App frame](../../app/src/components/AppFrame/AppFrame.tsx) | Currently displays the story timeline only for a non-empty selected Story. Studio projects need a meaningful empty composition surface. |
| [Generation versions](../../backend/services/versions.py) | Preserve originals and pin versions in Stories. Processed versions may replace their audio files; studio snapshots cannot rely on those files remaining immutable. |
| [Generation orchestration](../../backend/services/generation.py) | Combines inference, profile lookup, history, and publication. It is not a standalone stateless inference service. |
| [Engine registry and protocols](../../backend/backends/__init__.py) | Useful reuse boundary for model selection, inference, and GPU compatibility. Capability declarations require expansion and validation. |
| [Database models](../../backend/database/models.py) | Stories, generations, versions, and an existing generic Project JSON record already exist. Their persisted meanings must be preserved. |
| [Legacy migrations](../../backend/database/migrations.py) | Assume a single-user desktop SQLite database. Studio persistence needs explicit versioned migrations and recovery. |
| [Generation queue](../../backend/services/task_queue.py) | In-memory serialized work prevents ordinary queue contention; it does not provide durable studio jobs or restartable request ownership. |
| [Story export](../../backend/services/stories.py) and [generation packages](../../backend/services/export_import.py) | Audio mixing and individual generation packages exist. A complete project archive must capture the entire studio graph and its owned assets. |
| [Native configuration](../../tauri/src-tauri/tauri.conf.json) | Retains upstream bundle identity, sidecars, and updater configuration. Display branding is separate from a future independent distribution migration. |

Existing dictation, captures, native audio routing, profiles, effects, REST APIs, and MCP functionality remain supported. Studio implementation must preserve their behavior and public contracts unless a later migration explicitly specifies a change.

## 3. Design references

These are references for specific interaction patterns, not dependencies or claims that their implementations can be copied.

| Reference | Pattern to adapt | Application here |
| --- | --- | --- |
| [Descript editor](https://help.descript.com/descript-tour/the-editor-interface) | Script, preview, timeline, contextual panels | A script-led Edit workspace with detailed timing available when needed. |
| [ElevenLabs Studio](https://elevenlabs.io/docs/eleven-creative/products/studio) | Paragraph-level history, locks, chapters, performance direction | Stable passages with selectable takes and visible generation provenance. |
| [Murf Studio](https://help.murf.ai/murf-studio-basics) and [timeline](https://help.murf.ai/timeline-introduction) | Script blocks and progressive disclosure | Keep simple narration approachable; reveal media and timing surfaces in context. |
| [Typecast emotion controls](https://help.typecast.ai/en/articles/15347246-applying-emotion) | Direction attached to a selected passage | Make delivery intent visible, conditional on engine capability. |
| [Fairlight](https://www.blackmagicdesign.com/products/davinciresolve/fairlight) | Precise waveforms, take layers, mixer, automation | A focused Mix workspace; advanced comping and automation follow basic delivery. |
| [Frame.io review](https://frame.io/features/review-and-approval) and [comparison](https://help.frame.io/en/articles/9952618-comparison-viewer) | Feedback and comparison attached to revisions | Local review first; external reviewers and shared links later. |
| [Vapi call logs](https://docs.vapi.ai/observability/logs/call-logs) | Recording, transcript, outcome, cost, latency | Reference only for a separately scoped calling workspace. |

The initial visual language follows the supplied DoubleSharp wordmark: a near-black shell, white typography, light letter spacing for identity text, and controlled cyan/blue/violet/rainbow accents. Retain existing voice artwork and light-theme support, and use the supplied logo without altering its artwork. Professional polish comes from stable hierarchy, readable controls, predictable selection, and clear saved/reviewed states.

## 4. Scope and release sequence

| Phase | Included outcome | Deferred boundary |
| --- | --- | --- |
| B0: edition identity | Voicebox DS display naming; DoubleSharp splash, sidebar, and settings treatment; clear upstream attribution | New app identifiers, data migration, signing keys, updater infrastructure, and public release. |
| S1: professional studio | Projects; script passages and cast; capability-aware direction; audition and take selection; durable generation; recoverable edits and checkpoints; basic mixing/mastering; local review; audio delivery and project archive | Public sharing, translation, model research, remote inference, and phone calls. |
| S2: video production | Reference video and scene targets; pronunciation dictionaries; captions; bounded batch generation and authenticated automation | Full video editing, direct platform publishing, and unsupported third-party connectors. |
| S3: client and team workflow | Timecoded comments, explicit reviewer identity, review copies, revision comparison, permissioned sharing when hosting is approved | Simultaneous multi-user timeline editing is not part of the initial collaboration slice. |
| S4: qualified extensions | Reference-performance conversion; localization; advanced comping/retiming; explicitly paired render machines | Each requires a focused capability/design qualification before scheduling implementation. |
| Separate initiative | Qualification/booking voice agent and calling operations | Telephony, genuine incremental conversation, contact permissions, CRM, and campaign economics require their own spec. |

The first studio milestone is complete only when a project can be created, directed, recovered, reviewed, delivered, archived, and reopened. A new layout over the old generation history alone does not meet S1.

## 5. Information architecture and workspace layout

### Global navigation

- **Projects:** recent projects, search, client label, last modified, and project status; create blank, paste script, import Story, or import project archive.
- **Quick Generate:** retain the existing single-generation workflow and history.
- **Library:** Voices, Effects, and Captures remain directly reachable; Models and Settings remain global utilities.
- Existing URLs continue working during migration. Opening an old Story offers an explicit Create studio copy action.

Proposed project routes are `/studio/projects` and `/studio/projects/:projectId/:workspace`, with `edit`, `mix`, and `review` workspace values. Recent-project preference may choose the landing page without invalidating the existing `/` route.

### Project shell

The persistent header contains project name, saved state, checkpoint selector, running-job count, workspace switcher, and Deliver. Saving, generation, review, and export are separate states. Switching workspaces retains selection, playhead, undo history, scroll position, and job ownership.

| Workspace | Main surface | Supporting surfaces |
| --- | --- | --- |
| Edit | Script passages with speaker, take state, and lock state | Left outline/cast/assets; right Voice, Direction, Takes inspector; collapsible bottom timeline and persistent transport. |
| Mix | Timeline and waveforms | Track controls, selected clip/track inspector, optional mixer, master meters. |
| Review | Explicit immutable checkpoint or rendered deliverable | Read-only script, local notes/checklist, revision selector, local approval action. External reviewer controls arrive in S3. |
| Deliver action | Dialog or side sheet, retaining project context | Source revision, preflight, output preset, destination, progress, and audition of the written file. |

An empty project displays Paste a script, Add passage, and Import audio actions plus an intentional timeline drop target. It does not show an empty history table as the main task.

### Responsive behavior and interaction

- At desktop widths of at least 1200 CSS pixels, use resizable outline and inspector panels. Proposed initial widths: outline 220 px, inspector 300 px; maintain a readable central script.
- Between 800 and 1199 CSS pixels, collapse the outline and move the inspector into a dismissible drawer when needed. Preserve the transport and primary action.
- Below 800 CSS pixels, use one main panel at a time. Review and transport remain usable; simultaneous desktop mixing is not a mobile claim.
- Save panel dimensions per workspace locally and provide Reset layout. Validate stored sizes against the current viewport, including 200% zoom.
- Select a passage to reveal its clips and inspector. Select a clip to reveal its originating passage; imported audio can have no passage.
- Resizers have keyboard operation and separator semantics. Dragging, trim, reorder, and timeline placement have equivalent commands or numeric inputs.
- Use text/icons as well as color for pending, failed, stale, locked, and approved states. Support focus visibility, screen-reader labels, reduced motion, and both existing themes.

### Keyboard contract

Cmd/Ctrl+K opens the command palette. Space toggles transport only outside editable fields, menus, and dialogs. Cmd/Ctrl+Z and redo apply to the focused text editor when it owns an active text edit, otherwise to project history. Timeline S (split), Cmd/Ctrl+D (duplicate), and Delete operate only with timeline focus and an eligible selection. Global handlers must recognize `contentEditable` as well as input/textarea controls. Closing a drawer or dialog restores focus to its opener.

## 6. S1 functional requirements

### P1. Projects, script, and voice identity

1. A project has a stable identity, name, optional client label, language defaults, cast, ordered passages, tracks, clips, mix configuration, revisions, jobs, and deliverables.
2. Pasting a script previews its split into paragraphs/passages before committing. Speaker assignment is explicit; automatic speaker inference is optional later.
3. Each passage has a stable ID independent of its position. Reordering does not lose its takes, notes, or timeline links.
4. Cast assignments snapshot the selected profile configuration and any reference audio needed for regeneration. Editing a global profile does not silently change existing projects.
5. Model selection shows actual readiness and supported languages. Missing/incompatible voices require reselecting or relinking; no silent voice substitution.
6. Source script and spoken script are distinct when a user explicitly requests a rewrite. Show and accept the proposed wording before synthesis. Existing personality rewriting is off by default for studio narration.
7. Generation does not lock the script editor. Editing text preserves prior audio and marks the relationship as Script changed; audio needs generation.

### P2. Capability-aware direction and audition

1. The backend advertises capabilities per engine, model variant, and platform. The UI does not maintain an independent universal emotion capability list.
2. Capability data distinguishes delivery instructions, supported expressive tags, supported attributes, voice-design prompts, seed support, language support, and qualified streaming/performance-conversion support.
3. Current base Qwen configurations declare `supports_instruct=False`; CustomVoice and VoiceDesign configurations declare support. Preserve this distinction. A voice-design description defines a voice and is not interchangeable with a per-line delivery instruction.
4. On engine changes, incompatible direction stays visible as inactive project metadata. It is excluded from the synthesis payload. Unsupported expressive tags must not become literal spoken words without a user-visible resolution.
5. Commercial-use presets exclude models marked non-commercial or with unresolved suitability. The current OmniVoice non-commercial label must remain visible; adding an engine requires reviewing its model and dependency terms separately from the app's license.
6. Generate one take or an explicitly bounded comparison batch of up to three takes per selected passage. Show the number of jobs before submission; generate alternatives with distinct request identities.
7. A take exposes the script and voice/settings snapshot that produced it. Users can name, annotate, audition, select, and retain alternatives.
8. A/B audition can temporarily match listening loudness. This does not rewrite source files, project gain, or delivery mastering.
9. Generate and use is an explicit adoption intent. A completed job may adopt its take only when the passage input revision and selection token still match and the passage is unlocked. Generate alternative never replaces the current selection automatically.
10. Passage locks prevent text, direction, voice, and selected-take changes until explicitly unlocked. Locks are editing controls; checkpoint immutability is a separate storage guarantee.

### P3. Timeline and take use

1. Clips have their own stable IDs and pin an immutable source asset. Splitting or duplicating a clip does not duplicate or mutate its source.
2. Insert selected passages places their selected takes in script order at the playhead, or at the end when Append is chosen. Default inter-passage gap is a proposed editable 200 ms. Insertion is one undoable command; unresolved passages prevent insertion instead of being skipped silently.
3. V1 supports explicit placement, source trim, split, duplicate, gain, fades, track mute/solo, and overlap. Precise word-level replacement, phrase comping, and pitch-preserving retiming are S4 extensions.
4. Use take changes a passage's selected take. If existing clips use an earlier take, offer Replace linked clips or Keep timeline audio; show the resulting difference in the passage. Never silently change fixed clip starts.
5. Replacing linked clips is atomic. Preserve legal trim ranges; if the new audio is too short, ask the user to reset trims or keep the existing clips. Do not truncate an invalid source range silently.
6. Selection changes and timeline changes are ordinary project commands with undo. New audio from the model is retained independently of whether a selection command is later undone.

### P4. Saving, history, and checkpoints

1. Server-acknowledged project commands are durable. The header says Saved only after the durable commit is acknowledged.
2. Debounce normal typing into logical commands, targeting a save request within 500 ms after idle and no more than two seconds of continuous typing between journal submissions. These are proposed product targets, not measured current behavior.
3. Maintain a local recovery journal for unacknowledged edits. A backend outage shows Save failed/Reconnecting and preserves text, with explicit retry and recovery/export options. Do not claim unacknowledged keystrokes are guaranteed durable before local storage succeeds.
4. Commands require the expected project revision. Concurrent windows cannot overwrite a newer draft without detecting the conflict. Present Keep local recovery copy and Reload saved draft; automated merge is outside S1.
5. Persist undo/redo history across restart. In-flight effects and inference are not undone as physical operations; undo changes project references and selection intent.
6. A named checkpoint captures a complete immutable project state, pinned asset references, voice snapshots, and render settings. Restoring it creates a new draft head and history entry, preserving the checkpoint and previous approvals.
7. Draft/checkpoint deletion does not immediately delete audio. Explicit storage cleanup first computes references from drafts, checkpoints, history, jobs, and active renders and presents a recoverable deletion policy.

### P5. Mixing and mastering

1. Preserve native source sample rate, channel layout, sample count, and clean audio. Use an explicit 48 kHz stereo project working format by default for video-oriented jobs; resampling does not restore fidelity missing from a source.
2. Define timeline positions and processing boundaries in integer project samples. API timing inputs in seconds/milliseconds use one shared documented nearest-sample rounding rule; archive values retain the authoritative sample positions.
3. Map mono voice sources to both stereo channels without an implicit change to clip gain; preserve stereo media. Support source/master gain, fades, basic existing effects, and master limiting. Show dry/processed audition clearly.
4. Expose integrated loudness and true-peak measurements for a completed render. Proposed delivery defaults are -16 LUFS integrated and a -1 dBTP ceiling; these are editable product presets, not universal platform requirements. Silence and insufficient measurable speech display Not measurable instead of a fabricated zero.
5. Existing RMS normalization and peak clipping are not sufficient evidence of measured loudness/true-peak compliance. Qualify a metering/limiting implementation against independent audio fixtures before claiming those capabilities.
6. Mixing creates derived audio; it never regenerates speech or changes clean takes. Processing changes invalidate affected cached renders and their measurements.
7. Interactive preview may use a lighter processing path only when labeled Preview. A final-quality audition uses the same render pipeline as delivery. Users can audition the actual exported file.
8. One playback owner arbitrates passage audition, timeline playback, media preview, and native output routing. Starting an exclusive audition stops the previous source; Stop halts every source owned by that playback session. Navigation must not leave hidden playback or unrelated stale callbacks running.
9. Automatic music ducking, detailed automation lanes, advanced restoration, and third-party audio plugins follow S1. Retain existing effects without presenting these extensions as already supported.

### P6. Local review and delivery

1. Review opens a specific checkpoint, never an ambiguous mutable Current project. The read-only view shows the revision name, script, selected audio, and render settings.
2. S1 provides local notes/checklist and a self-approval action. It does not claim external client approval or team collaboration. Approval records bind to a revision and identify their local origin.
3. Later draft changes do not modify the approved revision. The header distinguishes Draft changed from the named approved checkpoint.
4. Deliver freezes a complete revision and performs preflight for missing assets, unresolved source ranges, absent narration takes, stale script/audio, and unsupported encoders.
5. Stale script/audio requires regeneration or an explicit Use earlier recording acknowledgement stored with the revision. The exported script/manifest records the actual take text. Never silently export older words as if they matched the current script.
6. S1 delivers final mix, per-voice dry/processed stems, and the complete project archive. Stems align at project time zero. Dry stems retain arrangement, trims, clip gain, and fades while bypassing effects; raw untrimmed takes remain available through the project archive.
7. Default final audio is 48 kHz stereo 24-bit PCM WAV. MP3 is available only when an encoder is installed and qualified. Keep legacy M4B export in the legacy workflow; studio chapter-aware audiobook delivery is a later preset.
8. Output names include project, revision, and output type. Default to a new destination filename. Replacing an existing deliverable requires an explicit overwrite choice and atomic replacement after successful rendering.
9. Report completion only after the output is successfully published at the chosen destination. Cancellation or write failure leaves prior deliverables intact and exposes an actionable error.

### P7. Import, archive, and recovery

1. Create studio copy imports an existing Story's pinned versions, selected audio, overlaps, trims, and gains without changing the Story or its source files.
2. Copy referenced legacy audio into studio-managed immutable storage before publishing the imported project. Legacy profile/generation/version IDs are provenance, not ownership or lifetime guarantees.
3. A portable archive contains a versioned manifest, project state, named checkpoints and their required assets, cast configuration/reference material selected for inclusion, take provenance, and relative asset paths with hashes.
4. Omit model weights and disposable caches from archives. Explain that playback needs the included audio; regeneration may need the recorded model to be available again. Record export scope when alternative takes/history are intentionally excluded.
5. Archive opening stages and validates the whole import before committing a new project identity. Validate schema version, hashes, referenced assets, sizes, file types, safe relative paths, and extraction limits. Reject traversal, absolute paths, symlink escapes, and missing required audio.
6. Missing optional regeneration references do not prevent playback of included audio. Missing required timeline audio prevents a successful project import; provide an explicit diagnostic instead of a partially valid project.
7. Round-trip qualification must reopen the archive on a clean application data directory with no legacy database or model cache, preserving reviewed playback and delivery settings.

## 7. Owned data and service boundaries

### Persistence ownership

Add a `studio` domain with a separate `studio.sqlite` and managed asset area beneath the application's selected data root. Keep upstream SQLite records and the existing generic `Project` JSON table unchanged. Use explicit numbered transactional migrations, backup before structural migration, and reject unsupported newer schemas with a recovery path.

The initial backend process can host both domains. A separate runtime process is a future optimization, not a prerequisite. No cross-database foreign keys are required; importing legacy data establishes ownership through copied assets and snapshots.

| Record | Required meaning |
| --- | --- |
| StudioProject | ID, metadata, schema version, current draft revision, working audio format, mix settings. |
| Passage | Stable identity, ordering, text, language, voice snapshot, direction, input revision, selected take, lock state. |
| VoiceSnapshot | Profile provenance, engine-specific voice definition, reference asset IDs, defaults, recorded model/capability identity. |
| Take | Immutable audio asset, frozen generation request, model/runtime identity, actual seed where known, source text, processing lineage, creation job. |
| Clip / Track | Stable clip ID, pinned source, sample-domain placement and trim, track, gain, fades; track type and mute/solo. |
| ProjectCommand | Idempotent operation identity, expected/committed revisions, validated change, undo data, journal position. |
| ProjectRevision | Immutable document state, parent revision, label, referenced assets, render recipe. |
| StudioJob | Frozen input, idempotency/request hash, target passage and selection tokens, execution state, attempt, cancellation intent, result/error. |
| StudioAsset | Hash, managed relative path, size, source format, sample rate/channels/sample count, publication state. |
| Render / Deliverable | Source revision/hash, complete render recipe, source asset hashes, measurements, output hash/path, publication state. |
| LocalApproval | Immutable revision ID, local attribution, timestamp, review notes; separate from external approval introduced later. |

### Engine adapter

Proposed module boundaries are `backend/studio/` for the owned domain and `app/src/features/studio/` for its UI. The adapter calls existing engine/model-loading code behind an application-owned contract. It must freeze reference material and generation settings rather than look up changing profile defaults while a job runs.

The adapter receives a validated request and an exclusive execution slot and returns audio, sample rate, actual settings/capabilities, and structured failure information. Studio code owns take creation and asset publication. Existing REST/MCP flows keep their compatibility behavior. Shared GPU arbitration must cover both legacy and studio requests; two independent queues must not run conflicting physical inference concurrently.

### API semantics

Use a versioned `/studio/v1` namespace, separate from existing generated client contracts. Exact transport schemas are finalized with the first implementation slice; these behaviors are mandatory.

| Operation | Proposed route and behavior |
| --- | --- |
| Project list/create/read | `GET/POST /studio/v1/projects`, `GET /studio/v1/projects/{id}`. Stable IDs and current revision tokens. |
| Project command | `POST /studio/v1/projects/{id}/commands` with operation ID, expected revision, command type, and payload. Validate and commit atomically. |
| Undo/redo | Command operations governed by the same concurrency and persistence rules. |
| Checkpoint/restore | `POST /studio/v1/projects/{id}/revisions` and a restore command. Restoration creates a new draft. |
| Capability/readiness | `GET /studio/v1/engines`. Include readiness, supported controls, limits, and the registry revision used to validate requests. |
| Generation submission | `POST /studio/v1/projects/{id}/generation-jobs`. Return `202` with a stable job ID only after request persistence succeeds. |
| Job read/cancel/retry | `GET /studio/v1/jobs/{id}` and explicit cancellation/retry actions. Retry creates a recorded attempt using the frozen input. |
| Render/archive/import | Durable job operations; expose destination publication separately from rendering progress. |
| Events | Project/job events include identity, sequence/version, and persisted state references. SSE is a notification optimization; reconnect always reconciles through read endpoints. |

The same operation/idempotency key with the same canonical request returns its original committed result. Reusing it with different content returns `409`. Revision conflicts also return `409` with the current head token; validation failures return structured `422` errors. A replayed result identifies its committed revision and cannot be mistaken for the latest project head.

### Job lifecycle and stale results

Persist `queued`, `loading_model`, `generating`, `processing`, and terminal `completed`, `failed`, `cancelled`, or `interrupted` states. Cancelling is a visible intent while physical work winds down; it is not proof that GPU execution has stopped.

- Freeze text, voice/reference assets, direction, engine/model identity, passage input revision, and selection intent at submission.
- Completion stores a new take. Adoption additionally compares the captured passage and selection tokens and checks that the passage exists and is unlocked. Late or superseded results remain alternatives with an explanation.
- Undo/redo, restore, selection changes, and passage deletion invalidate any affected adoption token, including when text later happens to match again.
- A model failure preserves previous selected audio. A retry uses the frozen request; a creative variation is a new request.
- On restart, reconcile published assets and job records. Resume unstarted queued work. Mark work interrupted during inference and require explicit retry unless successful publication can be proven from its recorded identity.
- Do not promise exactly-once physical inference. Guarantee idempotent request/result publication and prevent duplicate adoption.
- Keep the shared execution slot until a blocking/native inference call has actually ended, even if its awaiting coroutine was cancelled. A late cancelled result cannot become the selected take.

### Asset integrity and deletion

Publish by writing to task-owned staging, closing and validating the audio, computing its hash, and atomically moving it into managed storage before committing references. Reconcile an interrupted move/commit on startup. Do not label truncated or partially written audio as a completed take.

Garbage collection considers active drafts, named revisions, undo/redo history, generation inputs/results, review copies, and active render leases. Legacy delete/profile cleanup must never delete studio-owned assets. Studio checkpoints must not refer to mutable legacy version paths.

Persistent user project assets live in the configured application data location. Disposable builds, waveforms/captures used as engineering evidence, benchmarks, and test fixtures generated during development follow the repository's Scratch-volume instructions. No run evidence or model weights belong in Git.

## 8. Video production and later extensions

### S2: video voiceovers and automation

- Attach a reference video with explicit project frame rate and time origin. Scene targets are intervals; display actual selected-take duration against the target.
- Offer manual pacing/placement and a proposed shorter script for review. Do not silently rewrite narration or over-stretch speech to satisfy timing.
- Store pronunciation rules by project/voice with engine-specific alias or phoneme support. Show the resulting spoken text where substitutions occur.
- Export SRT/VTT from alignment against the final selected and trimmed audio. Preserve cue provenance and invalidate cues when audio, trims, arrangement, or wording changes. Low-confidence alignment is reviewable; word-perfect alignment is not assumed.
- Add batch manifests with project/passage IDs, voice snapshot, intended output, job identities, bounded concurrency, resumable status, and explicit retries.
- Offer completion notifications/polling and a documented authenticated interface for external automation. Never turn the existing unauthenticated local API into a publicly exposed production integration.
- A folder/API handoff is the initial animation-tool integration. A named Grokbots connector requires verified provider capabilities; no provider integration or YouTube publishing is implied by this spec.

### S3: review and collaboration

External comments bind to a review copy and immutable revision, with timecodes, reviewer identity, resolved state, and comparisons between revisions. Sharing an audio review copy does not share voice references or generation credentials by default. Define access, expiry/revocation, retention, and required user actions before implementing hosted links. Self-hosted operation is a supported design direction; no hosting vendor is selected here.

### S4: capability-qualified extensions

| Extension | Observable outcome | Qualification before implementation commitment |
| --- | --- | --- |
| Reference performance | Record a rough reading and guide a target voice's delivery | Qualify a speech-to-speech/performance-conditioning engine for supported voices, languages, fidelity, hardware, and distribution terms. Ordinary TTS instructions do not prove this. |
| Localization | Side-by-side translations, cast, glossary, timing, and language review | Preserve source-to-translation revision links, flag changed passages, and require language review. No guaranteed identical voice across languages. |
| Advanced comping/retiming | Combine preferred phrases and adjust timing without damaging approved sources | Qualified boundaries/alignment, crossfades, and pitch-preserving processing with audible checks. |
| Remote render worker | Editing laptop submits to an explicitly paired user-owned workstation | Authenticated pairing, explicit processing location, version/capability negotiation, resumable jobs, artifact integrity, revocation, and network-loss behavior. No hidden cloud fallback. |
| Voice identity library | Reuse a narrator or character consistently across jobs | Versioned voice definitions, reference performances, project opt-in to updates, and an engine-specific compatibility policy. |

Calling requires its own contact/consent model, telephony transport, incremental STT/TTS, turn detection, interruption handling, tool/CRM/calendar actions, human handoff, testing, and per-outcome economics. The current endpoint named stream sends fully generated WAV data and is not evidence of real-time conversational readiness. No calling controls appear in the S1 studio.

## 9. Edition branding and distribution

The immediately requested branding uses **Voicebox DS**, a **DoubleSharp Edition** badge, and **Enhanced by DoubleSharp** attribution. The user supplied the DoubleSharp website and original rainbow-flower/white-wordmark asset as the visual reference. Splash/startup, sidebar, browser/document titles, and Settings/About should read as one restrained identity, with dark neutral surfaces and cyan/blue/violet accents; retain a readable light theme. Use the supplied full wordmark in splash/About and its flower through CSS presentation for compact marks. Retain upstream artwork and creator/license credit, link the DoubleSharp website from the signature and the fork from edition information, and describe existing enhancements without advertising unimplemented studio capabilities.

Startup messaging describes actual connection/preparation state; decorative text must not imply that training or model fine-tuning is occurring. Animation respects reduced-motion preferences. Error states retain readable diagnostics and recovery actions.

A future independent public distribution must separately establish application identifier, data migration, signing/notarization, release channels, update verification, installer names, and sidecar discovery. The branding pass alone must not move existing user data or claim that those distribution gates have passed. Upstream update settings remain a release-policy concern before any independently branded installer is shipped publicly.

Keep the upstream Git history and recorded baseline. Own studio modules and migrations; review upstream fixes with dependency context at chosen release boundaries. Model adapters, GPU/runtime compatibility, and packaging fixes remain valuable inputs. No blanket promise of full upstream feature compatibility is made.

## 10. Acceptance and proof boundaries

These are requirements for future implementation, not checks completed while writing this document.

| ID | Acceptance scenario | Required proof |
| --- | --- | --- |
| A01 | New user pastes a three-passage script, selects a preset voice, generates alternatives, chooses takes, arranges them, and delivers audio. | Real UI journey plus actual output-file inspection. |
| A02 | Import a Story containing pinned versions, splits, overlaps, trims, and gains. The new project preserves them and the Story is unchanged. | Database/file assertions and audible preview/render comparison. |
| A03 | Edit text, change selection, lock/delete a passage, undo, or restore while inference is active. Old completions never supersede newer intent. | Deterministic lifecycle tests plus one real-engine race journey. |
| A04 | Retry identical submissions and replay reconnect events. One logical request/result is adopted once. | API/idempotency and persistence tests, including changed-body conflicts. |
| A05 | Restart after an acknowledged edit; recover a failed save; undo/redo after restart; restore an older checkpoint. | Persistence/recovery tests and desktop restart journey. |
| A06 | Delete a legacy generation/profile or modify a legacy processed version after importing it. Studio checkpoints still play. | Cross-domain asset-retention tests with real files. |
| A07 | Cancel during native inference; submit legacy and studio work together. No overlapping conflicting inference or late cancelled adoption occurs. | Worker arbitration/lifecycle tests and supported GPU runtime evidence. |
| A08 | Mix mono/stereo sources with different sample rates, overlaps, trims, gain, and fades. Preview and export use the frozen arrangement. | Independent sample-domain fixtures, metering fixtures, and listening checks. |
| A09 | Review/approve revision R, edit the draft, then deliver R. Approval and audio remain tied to R. | Revision/render invariants plus UI journey. |
| A10 | Export fails or is cancelled after encoding begins or while writing the destination. An older deliverable remains intact. | Failure injection at publication boundaries and file readback. |
| A11 | Archive/import in a clean data directory with models unavailable. Approved audio plays; missing regeneration dependencies are explained. | Artifact round-trip, asset/hash validation, and clean-profile playback. |
| A12 | Import corrupt/unsupported archives, traversal paths, symlinks, oversized payloads, or missing required assets. | Parser/extraction tests proving no out-of-root writes or partial publication. |
| A13 | Keyboard-only and screen-reader use reaches passage editing, take selection, numeric timing, review, and delivery. Narrow layouts and 200% zoom retain actions. | Accessibility/component checks and rendered interaction checks. |
| A14 | Switch audition/timeline/native output, stop, and navigate. No duplicate or hidden playback survives. | Playback ownership tests plus native output/device runtime checks. |
| A15 | Existing Generate, Stories, Captures, Voices, Effects, REST, and MCP workflows remain usable. | Focused existing regressions and affected native journeys. |
| A16 | Splash, sidebar, titles, and settings identify the DoubleSharp edition while retaining upstream attribution. | Source/type checks, rendered visual check when authorized, and packaged identity check before release. |

Record performance on an explicit reference machine using a proposed workload of 100 passages, 200 clips, and a 30-minute project. Measure editor responsiveness, memory, waveform loading, and render resource use; do not invent generation-speed or GPU-concurrency claims. Interactive editing must not wait on inference; long audio loads and renders must remain cancellable and visibly progressing.

Source tests, browser/rendered behavior, actual model output, database migration, native playback, packaged installation/signing, and external provider operation are separate gates. Passing one does not qualify the others. Engineering evidence belongs on Scratch; only concise maintained conclusions belong in repository documentation.

## 11. Decisions remaining before dependent work

1. Confirm the first supported OS/GPU/model matrix and reference hardware; qualify actual adapters before advertising supported direction controls.
2. Confirm the independent distribution identity and update strategy before shipping an installer beyond the existing local development context.
3. Choose and qualify metering/limiting, archive limits, command-history retention, and recovery-journal storage during their implementation slices. The behavioral contracts above remain mandatory.
4. Decide whether S2 video preview is required for the first commercial milestone or follows the core audio studio. The proposed sequence keeps it separate to make S1 verifiable.
5. Select collaboration hosting, account/identity, and retention only when S3 is scheduled. Existing cloud roadmap text is context, not an infrastructure dependency or deployment authorization.
6. Schedule performance conversion, localization, and calling only after their separate capability and product uncertainties have been resolved.

These decisions do not prevent reviewing this specification or completing the authorized edition-branding pass.
