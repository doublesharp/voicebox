# Voicebox DS: information architecture and layout refresh

Status: Opus 5.5 consultation completed before further application layout changes. Implemented, installed and checked in the native app on 29 October 2026.

## Goal

Make Voicebox DS feel like a focused, professional voice production app. A person should immediately understand where to start, what is selected, what an action will change, and how to recover when something needs setup. Keep DoubleSharp character through the rosette, color and audio graphics, with a calm working canvas.

The main tasks are quick speech generation, managing voices, and assembling a project for delivery. Captures are a separate input workflow. Existing Stories and all saved data remain usable. This refresh does not add a calling service or make new claims about engine capabilities.

## Delivery checks

- Native checks exercised the 1200 by 800 desktop layout and the 800 by 600 minimum size, compact navigation, voice sheet, unsaved edit guard, Create Voice scrolling, Effects, Models, Captures readiness and speech settings. Light appearance was checked and the original System preference restored.
- The native pass found and corrected stacked Create Voice sections overlapping at compact widths, misleading zero voice totals from the detail endpoint, and truncated Effects headings and counts. Switching among existing 5-, 20- and 4-second clips loaded the matching selected clip and duration.
- The final frozen frontend passes 373 tests in 56 isolated processes and TypeScript checks. Seeded action reach assertions and a separate source-mapped coverage runner are included. See [testing coverage](../testing-coverage.md) for the whole-frontend denominator, branch statistics and remaining gaps; 95% across the frontend has not been reached.
- The signed app and bundled Python framework verify. Startup health is healthy; the existing voice and five generation IDs are preserved. No new model generation or permission grant was needed for this UI pass.
- A populated project workspace was not exercised in the native app because this local database has no projects. Studio component and lifecycle tests passed; they are separate evidence from native acceptance of a populated project.
- Publishing merged this snapshot with the existing upstream reliability and preview fixes on DoubleSharp. The combined source passes 414 frontend tests, 98 focused pytest tests and two Rust startup tests, plus app/web/desktop TypeScript checks and both frontend production builds. The native checks above apply to the installed snapshot; the combined merge has not been repackaged for native runtime acceptance.

## Observed problems

- The icon-only rail makes eight equally weighted destinations hard to learn. Projects and Stories offer overlapping ways to arrange audio without explaining the relationship.
- Generate gives large voice cards most of its left column while script entry is a floating panel near the bottom. Take cards put metadata beside a cramped transcript. Selection, playback and editing have competed for the same click region.
- Inspector forms look like a long stack of generic inputs beneath a large avatar. Related settings have little hierarchy; useful save controls can fall below the scroll area or player.
- At the supported 800 by 600 minimum window size, voice-table headings overlap and header controls squeeze. Fixed composer offsets assume an 80-pixel rail and 360-pixel story pane.
- Some controls rely on hover for discovery. Setup and model vocabulary can appear before a person understands their task. Captures can suggest an unavailable Dictate action.

## Recommended information architecture

Use visible destination labels on a 184-pixel navigation rail at windows at least 1100 pixels wide, with an accessible compact 72-pixel rail below that. Project workspace routes always use the compact rail so the script keeps its working width. Separate primary work from supporting tools with a small section label and space. Do not hide the active destination.

| Navigation | Purpose | Existing route and compatibility |
| --- | --- | --- |
| Projects | Longer voiceovers: script, performance, arrangement and delivery | `/studio/projects` and existing project routes |
| Generate | One-off speech and quick auditions | `/` |
| Voices | Voice library, reference samples and voice defaults | `/voices` |
| Captures | Record or import audio and work with transcripts | `/captures` |
| Effects | Reusable effect presets | `/effects`, supporting tools group |
| Models | Downloads, readiness and storage | `/models`, supporting tools group |
| Settings | App, generation and capture configuration | Existing `/settings/*`, bottom utility |

Keep Stories as a clearly named secondary destination, accessible from Projects and when its existing route is active. Explain “Stories are the earlier audio arrangement workspace. Create a project copy to use Studio.” Preserve its routes, selection, generation and export behavior. Do not migrate or delete data automatically.

Inside a project, retain the existing Edit / Mix / Review workspaces and Deliver action. Strengthen the visible descriptions (“Script & takes”, “Arrange & mix”, “Review & approve”) rather than introducing new workspaces unsupported by the current state model. Keep project title, save state and current workspace together in the header. Keep delivery clearly available without competing with Generate.

## Screen layouts

### Shared shell

- Navigation, page heading, contextual actions, work area and transport each have one clear place.
- Use `--app-rail-width` and `--app-player-height` layout tokens instead of scattered offsets. Update router content, floating composers, legacy story pane offsets and player placement together. The player begins at the rail edge; remove the rail's bottom-padding workaround.
- Primary actions use the established accent consistently. Use quiet outlines for secondary actions and reserve destructive styling for actual destructive actions.
- At compact widths use explicit pane switching or a usable horizontally scrollable table; avoid two vertically stacked panes so short that neither can be used with the player open.
- Keyboard focus is visible, all icon actions have accessible names, and active navigation has correct current-page semantics.

### Generate

Make the text editor the main working surface, with a visible “Generate” heading and short task description. Group Voice, Language and Engine under a compact “Voice & delivery” section above or beside the script. Keep speed and delivery controls easy to reach, with advanced options collapsed.

Put recent takes in a clearly titled adjacent pane on wide windows; use a single-column layout or an explicit Recent takes toggle at compact widths. Keep metadata compact and let text use the available width. Give playback, favorite, version and overflow actions separate controls. Show selected and playing states distinctly. Keep the current source identity tied to the audio player.

Preserve the voice-selection and import/create journeys, memory-only text draft, selected engine/effects/speed, async guards and generation behavior. Do not persist private draft text to disk as part of this change.

### Voices and effects

Use a library-and-inspector pattern with a consistent panel header. A small avatar or effect icon accompanies the title, not a large decorative block. Voice sections are Profile details, Default effects and Reference samples. Effect sections are Preset details, Effect chain and Preview.

Fields have quiet 12-pixel labels, 14-pixel values, consistent spacing, soft panel backgrounds, restrained borders and clear validation. Pair short related fields when the actual panel width permits it; long text and effect chains use full width. Put useful hints next to the relevant decision.

Keep an always-reachable save area while a draft is dirty, with an Unsaved changes label. It must remain above the global player. Preserve typing during background refetches and requests. Use a readable, horizontally scrollable library table or compact list at narrower widths, with meaningful empty search results and keyboard-operable row selection.

### Create/edit dialogs and settings

Use a bounded, rounded dialog with a clear header, scrollable body and stable action footer. Wide voice creation uses source/sample controls and profile details as two coherent sections. At narrower widths they share one natural scroll region rather than multiple clipped nested panes. Validation must bring the failing control into view. Keep cancellation and draft-restoration behavior.

Group settings into small sections with brief introductions. Use bordered surfaces and readable rows for toggle-heavy configuration. Keep navigation scrollable with long translated labels. Avoid exposing implementation detail unless it helps someone make a choice.

### Projects, captures and models

Use direct page titles and one prominent next action. Projects should say Projects, with Create project, search and useful recent project cards; avoid oversized promotional copy in the working app. Use existing waveform graphics and subtle empty-state illustrations where they clarify the task.

Captures should explain missing prerequisites with the existing readiness checklist, even when the global shortcut is off. Show Dictate when it is usable; otherwise offer a clear setup action. Keep microphone permission and model downloads explicit user actions. Model cards should emphasize Ready, Downloading, Missing or Error with clear progress and retry behavior; avoid implying that a model is generating while merely downloading.

## Help, tooltips and graphics

- Add short inline help for reference samples, engine choice, generated speed, default effects, preset effect order, and source/version selection. Use existing capability data for factual guidance.
- Tooltips explain compact controls on pointer hover and keyboard focus. They supplement accessible names and do not carry essential instructions alone. Use existing Radix popovers for longer explanations and keyboard-operable help buttons.
- Use the DoubleSharp rosette in the rail and startup surface, existing waveforms for audio, and consistent line icons for navigation and actions. Avoid decorative graphs, stock imagery or animations in forms.
- Respect reduced motion, light and dark themes, and readable contrast. Keep loading and empty/error states clearly distinct.

## Delivery and verification

1. Obtain a substantive read-only Opus 5.5 critique of this proposal and the current source, then record decisions below before application layout changes.
2. Resolve the three material Terra review findings: refresh profile caches after effects saves; distinguish automatic profile effects from deliberate composer overrides; retain a created preset ID when later edits need a second save.
3. Implement the shared navigation/layout tokens and page hierarchy, then the Generate workspace and consistent library/inspector forms. Preserve existing routes and data contracts.
4. Extend focused tests for routing/selection, draft lifecycle, automatic versus explicit defaults, save/refetch ordering, keyboard actions and help accessibility. Keep deterministic seeded sequences bounded and reproducible.
5. Typecheck and build the frozen candidate; run relevant suites in isolated Bun processes to avoid cross-file mock leakage. Use Scratch for build output and evidence.
6. Verify the real macOS app at 1200 by 800 and 800 by 600, including player open, long text, dialogs, scrolling, focused controls, tooltips/help, empty states and dirty forms. Reuse the packaged backend; preserve voice/history data and unrelated source edits. Install only a verified local update.

The pre-refresh candidate passed 306 tests in 39 isolated frontend files. Its independent Terra review found three P2 issues above; it is not installed and is not accepted as complete. Source/test passes do not establish native visual or live mutation behavior.

## Opus consultation and decisions

Consultation: [Opus 5.5 review](t3-thread://v1/thread:delegated-task:command%3Amcp%3A4c8d0f50-6c91-4d7e-8103-8181602be128%3Adelegate-task%3Aeec7a9ce-f643-4a5b-9d65-8add4d1394ec), completed 2026-10-29. The source-only critique did not establish rendered appearance or native acceptance. The full response is retained on Scratch at `reports/ui-review-20261029/opus-5.5-ia-review.md`.

Decisions adopted:

- A 184-pixel labelled rail outside project workspaces, a 72-pixel compact rail below 1100 pixels, and a compact rail throughout project workspaces. Group Create, Tools and bottom Settings; show a nested current Stories entry when appropriate.
- Extract a shared `GenerateComposer` layout mode: docked in Generate, floating in Stories. Keep one form/state/submit implementation. Compact Generate has one natural page scroll and Recent takes below the composer.
- Show the selected voice in a compact picker rather than large profile cards. Preserve create/import paths and the zero-voice journey.
- Use an inspector header led by the item's name, meaningful facts, section grouping and a stable save footer. Compact Voices opens the inspector in a right-side sheet. Protect unsaved drafts across selection and closing.
- Add a small dependency-free, focus-accessible `Hint` and existing-Radix `HelpButton`. Replace hover-only bespoke tooltips. Essential guidance stays inline.
- Use restrained panels, 36-pixel form controls, 32-pixel dense controls, 8-pixel control radii and consistent button tokens. Preserve Studio's mature grid and state behavior.
- Give Projects a direct heading, one New project action, grouped import tools and a clear Earlier Stories explanation. Add descriptions or hints to Edit, Mix and Review.
- Keep waveforms where decoded/peak data already exists. Do not add per-row waveform decoding. Use text plus icons/status for take rows and models.
- Keep the existing click-to-audition history contract during this refresh; make playback and utility controls separately accessible rather than changing established click behavior without a separate user need.
- Treat Opus's per-step commit suggestion as a verification/build sequence only. No commit or push is authorized by this design review.
- Keep a narrow, persistent preset list on compact Effects and a 280-pixel capture list on compact Captures in this pass. These workspaces retain their full-height editor/recording surfaces and existing mounted request state. The new modal-sheet behavior is applied to Voices, with an explicit save/discard guard and a dirty-state breakpoint guard; do not introduce an unguarded unmount into the other active workflows.
