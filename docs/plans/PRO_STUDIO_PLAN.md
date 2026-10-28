# Voicebox DS: phased implementation plan

**Status:** B0 branding implemented. S1 implementation authorized by the user and in integration/review; runtime qualification remains open. S2-S4 remain phased follow-up work.
**Date:** 2026-10-28
**Specification:** [PRO_STUDIO_SPEC.md](PRO_STUDIO_SPEC.md)

This plan decomposes the specification into deliverable slices. Local Coordinate is the authoritative task and evidence record: Voicebox DS project `project_14a676bd-5cff-4ffe-ad9c-9aa08abc554e`, S1 effort `effort_efc6c236-4604-4cb2-95ee-f1e66c8e6320`. This file is a working view. Source implementation, fixture checks, native/model qualification, and distribution acceptance are separate gates.

## Working rules

- Preserve existing Quick Generate, Stories, Captures, Voices, Effects, REST/MCP, and native audio behavior.
- Keep new domain code under `backend/studio/` and `app/src/features/studio/`; use small explicit integration points in shared routing, database startup, engine access, and the application shell.
- Do not repurpose the existing generic `Project` JSON table or rewrite legacy Story data in place.
- Use task-owned Scratch directories for all disposable outputs. Confirm `/Volumes/Scratch` is mounted before builds, test captures, benchmark data, or generated audio. If unavailable, stop those operations.
- During future verification, use a task-specific `TMPDIR`, `PYTHONDONTWRITEBYTECODE=1`, and explicit Scratch test-cache/build paths where required. Do not use literal `/tmp` or create a second shared build/cache owner.
- Write behavior regressions before substantive behavior changes; avoid tests that merely duplicate static branding copy or implementation structure.
- Each slice includes its own API, UI, and persistence work needed for a reviewable user outcome. Native/model/release qualification occurs at its actual boundary.

## B0. DoubleSharp edition identity

**Requirement:** Spec section 9 and A16.

**Outcome:** App display surfaces identify Voicebox DS / DoubleSharp Edition, with clear DoubleSharp enhancement attribution and retained upstream credit. Follow the supplied DoubleSharp logo and website: near-black surfaces, white identity typography, cyan/violet accents, restrained rainbow details, and a readable light theme.

**Affected areas:** `app/src/App.tsx`, `Sidebar.tsx`, `ServerTab/AboutPage.tsx`, `ServerTab/GeneralPage.tsx`, a small shared branding component/constants module, supplied logo asset, theme tokens in `app/src/index.css`, document titles, and Tauri display configuration. Update edition-aware MCP installation examples and OS permission copy that depend on the display name. Preserve native identifiers/data paths, sidecar names, updater keys/endpoints, and runtime logic during this scoped pass.

**Verification:** Check changed copy and source consistency; focused TypeScript and Biome checks. When browser work is authorized, inspect splash, startup error, sidebar, settings, light/dark themes, reduced motion, and narrow layout. Before independent distribution, inspect packaged name and all packaging consumers of the product name.

**Dependency:** None. Separate from S1.

## S1.1. Owned project storage and immutable audio

**Requirements:** P1, P4, P7; A02, A05, A06, A11, A12.

**Outcome:** Create/read a studio project, apply revision-checked commands, snapshot/import legacy assets, and persist complete state without affecting upstream-owned records.

**Affected areas:** New studio database/models/migrations/assets/project services; integration in backend startup and route registration. Reuse legacy records only through a read/import adapter.

**Contracts:** Separate `studio.sqlite`; immutable hashed audio; stable passage/clip identities; operation idempotency; expected-revision conflict; write/validate/move/commit publication; no cross-database lifetime assumptions. Include persistent command journal and checkpoint asset reachability from the start.

**Verification:** Create/update/reopen, concurrent-window conflict, command replay, interrupted migration/publication, legacy deletion isolation, and asset-reference validation. Use isolated real SQLite/filesystem tests; no ML downloads needed.

**Dependency:** Agree the domain schema and adapter ownership described in spec section 7. Blocking schema/data decisions must be resolved inside this slice before later code relies on them.

## S1.2. Project shell and recoverable script editing

**Requirements:** P1, P4; A01, A05, A13.

**Outcome:** Create a project from pasted text, assign cast, edit ordered passages, navigate a persistent project shell, save/recover, undo/redo, and restore named checkpoints.

**Affected areas:** New studio routes/layout/script/inspector components; small integration in `app/src/router.tsx`, `Sidebar.tsx`, and the API facade. Keep existing editor routes accessible.

**Contracts:** Saved follows server acknowledgement; local recovery journal is distinct from committed state; conflict UI preserves local edits; selection and playhead survive workspace changes. Use the layout/shortcut contract in spec section 5.

**Verification:** Focused component tests for edit/save/conflict/focus behavior plus a real UI save/restart/recover journey. Verify the empty project, loading/error states, 800 px desktop width, 200% zoom, and keyboard interaction.

**Dependency:** S1.1.

## S1.3. Durable generation and shared runtime arbitration

**Requirements:** P2; job lifecycle in section 7; A03, A04, A07.

**Outcome:** Submit a persisted generation request, observe progress, receive an immutable take, cancel safely, and recover after restart without overwriting newer editing intent.

**Affected areas:** New studio job/engine adapter services and API routes; narrow coordination with `backend/backends/`, model loading, and existing generation arbitration. Preserve legacy API contracts.

**Contracts:** Server-authoritative capabilities; frozen voice/reference assets; request idempotency; passage/selection-token compare before adoption; exclusive physical inference slot shared with legacy work; explicit interrupted/retry state. SSE is not the sole record of completion.

**Verification:** Deterministic fake-engine lifecycle tests covering text edits, lock/delete, undo/restore, reconnect, duplicate submissions, failure, cancellation while blocking inference continues, and legacy/studio contention. Then qualify real preset/cloned/directed generation on each initially supported backend and packaged runtime. Fake engines do not establish model support.

**Dependency:** S1.1. Can develop alongside S1.2 after API/schema contracts are fixed.

## S1.4. Director, take audition, and timeline arrangement

**Requirements:** P2, P3; A01, A03, A13, A14.

**Outcome:** Generate up to three alternatives, compare them, explicitly choose a take, insert passages into the timeline, and make undoable trim/split/gain/fade edits.

**Affected areas:** Studio inspector/takes/timeline and a shared playback-owner interface. Reuse existing timeline/profile/effects components where their ownership contracts fit; do not fork a second uncontrolled native playback lifecycle.

**Contracts:** Unsupported controls stay inactive; model changes cannot read tags as words silently; audition matching is temporary; original audio is immutable; clip replacement validates trims and preserves fixed starts; passage selection and timeline source differences are visible.

**Verification:** Component and project-command tests, numeric-vs-drag equivalence, passage/clip linked selection, contentEditable shortcut handling, audition/timeline stop ownership, and one real-engine audition journey. Native device/channel output needs its own runtime checks.

**Dependencies:** S1.2 and S1.3.

## S1.5. Shared render recipe, mastering, and delivery

**Requirements:** P5, P6; A08, A09, A10, A14.

**Outcome:** Mix a frozen revision, inspect qualified loudness/true-peak measurements, render mix/stems, write outputs atomically, and audition the exported file.

**Affected areas:** Studio render recipe/audio-processing/measurement/delivery services; Mix workspace, meters, and Deliver sheet. Qualify dependencies before choosing implementation-specific DSP APIs.

**Contracts:** Explicit project rate/channels/sample rounding; clean sources preserved; source trims/gain/fades agree in preview/export; actual final render determines measurements. Preflight blocks missing audio and unresolved stale script/take relationships. No overwrite until full publication succeeds.

**Verification:** Independent sample-domain and metering fixtures, silence/short-audio handling, mixed native sample rates/channels, frozen revision identity, cancellation/disk failure, encoder absence, readback/hash, and audible comparisons. Document what preview approximates and what final-quality audition proves.

**Dependencies:** S1.1 and S1.4. Metering/DSP qualification may begin after render contracts are fixed.

## S1.6. Local review, portable archives, and migration qualification

**Requirements:** P4, P6, P7; A02, A05, A06, A09, A11, A12.

**Outcome:** Open a named checkpoint in Review, record local approval, import a legacy Story as a protected copy, and reopen the full project archive in a clean environment with no models installed.

**Affected areas:** Studio revision/review/archive/import services and UI; legacy import adapter; storage reachability/cleanup. Import/export uses staging and validated manifests.

**Contracts:** Local approval is not external collaboration; approvals never float to newer drafts. Archive scope is explicit; required audio is complete; optional model/reference dependencies are explained. Legacy paths/IDs remain provenance only.

**Verification:** Clean-profile round-trip and playback, corrupt/traversal/symlink/archive-limit cases, interrupted imports, stale/missing reference diagnostics, legacy source unchanged, deletion isolation, and restored revision exports. Models must remain unavailable during playback qualification.

**Dependencies:** S1.1, S1.2, S1.5. Archive validation work may start earlier; final qualification requires complete project/render semantics.

## S1.7. Integration and supported-platform release gates

**Outcome:** The A01-A16 acceptance scenarios applicable to S1 are demonstrated with clearly separated source, UI, database, audio/model, native, and artifact evidence.

**Checks:**

- Source/type/lint for touched packages; focused existing generation/version/queue/story/export regressions.
- Real UI create/direct/arrange/review/deliver/reopen journey, including save failures and crash recovery.
- Reference-project resource/performance measurements on named hardware; no unqualified generation-speed promises.
- Existing REST/MCP/captures/dictation and device/channel playback behavior.
- Installed/frozen build sidecar/model loading, startup, permissions, suspend/restart/stop behavior on the declared supported matrix.
- Separate independent-distribution identity/data migration/signing/notarization/updater plan before public installer release.

**Dependencies:** All S1 slices. Broaden tests only for shared-boundary changes or unresolved failures; repeat expensive model/native qualification only when relevant changes invalidate it.

## Prospective verification commands

These use existing tools; studio test filenames will be introduced with their owning slices. They are not a report of executed checks.

```sh
# Read-only source checks from repository root.
bunx tsc -p app/tsconfig.json --noEmit
bunx tsc -p web/tsconfig.json --noEmit
bunx biome check <changed-source-files>

# Use the existing backend environment and task-owned Scratch temp/cache paths.
# Select existing regression files and the new owning tests for the slice.
PYTHONDONTWRITEBYTECODE=1 TMPDIR=<scratch-task-temp> <existing-python> -m pytest \
  <owning-test-files> -p no:cacheprovider

# Frontend behavior tests use the existing Bun runner and owning test files.
TMPDIR=<scratch-task-temp> bun test <owning-frontend-test-files>
```

Do not install dependencies, bootstrap environments, download models, or trigger large builds merely to execute a documentation-only change. Verify actual command paths and output routing before each future implementation run. Production build/package commands need explicit Scratch output overrides and the platform-specific release workflow.

## Follow-on planning

After S1 is accepted, turn S2 into separate slices for reference-video timing, pronunciation/alignment/captions, and authenticated batch handoff. Plan S3 only after reviewer identity, sharing boundaries, and hosting are selected. Each S4 capability starts with a bounded qualification of its specific uncertainty. Telephone outreach remains outside these studio milestones.
