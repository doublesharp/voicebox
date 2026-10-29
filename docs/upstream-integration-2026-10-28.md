# Upstream integration — 2026-10-28

Eight focused upstream changes were integrated into the working checkout after isolated validation. This is a local source integration; it does not claim a release, model-quality acceptance, or a completed code review of the entire upstream queue.

## Sources and adaptations

| Upstream PR | Reviewed head | Contributor | Included behavior |
|---|---|---|---|
| [#1208](https://github.com/jamiepine/voicebox/pull/1208) | `fc5a0022800a` | @vincentfranstyo | Use the shared QueryClient in desktop/web; execute offline local requests and retain refetch-on-reconnect. |
| [#1204](https://github.com/jamiepine/voicebox/pull/1204) | `1a58e2505dbc` | @maxazure | Preserve explicit URLs across startup and migration; accept the discovered port for automatic URLs. |
| [#1219](https://github.com/jamiepine/voicebox/pull/1219) | `936a2e63d8a4` | @FelixKruger | Preserve decode diagnostics for uploaded files and recordings; retain the short-dictation notice. |
| [#1218](https://github.com/jamiepine/voicebox/pull/1218) | `87fbcae2c4c4` | @Tony25032 | Pass capture language into refinement; name all supported languages and use a same-language fallback for unknown codes. |
| [#1198](https://github.com/jamiepine/voicebox/pull/1198) | `6e10f90b8bfd` | @vincentfranstyo | Accept all 23 supported languages in STT request schemas and capture retranscription. |
| [#1199](https://github.com/jamiepine/voicebox/pull/1199) | `a694f3e8d7d2` | @vincentfranstyo | Explain the specific LuxTTS short-input vocoder error while preserving other RuntimeErrors. |
| [#1220](https://github.com/jamiepine/voicebox/pull/1220) | `ad2803f3ef9a` | @mvoloch | Exercise progress reporting above its existing 1 MB floor. |
| [#1180](https://github.com/jamiepine/voicebox/pull/1180) | `ad6c894cdfb1` | @Lvigentini | Use the platform-specific ffmpeg fixture name. |

#1204 needed a review correction: the exact legacy bundled address `http://127.0.0.1:8000` migrates to automatic selection in local/unspecified mode. Explicit remote mode preserves that address. Two new upgrade tests failed before the correction and pass afterward. The review reported no other P1/P2 findings in its bounded scope; the correction was self-verified.

## Validation

- `bun run test:upstream`: 26 tests across four files. Includes actual App startup effects, delayed discovery, legacy persistence migration, offline queries/mutations, reconnects, and actual capture-hook error callbacks. The script is wired into frontend CI.
- Offline Python regression run: 53 passed across STT language, refinement language, LuxTTS short-input handling, progress, MP3 export, existing refinement cleanup and reference-cache suites. New API tests use isolated real SQLite databases and a stub STT backend. Refinement tests verify prompt construction and persistence with a stub LLM.
- App, web and Tauri frontend TypeScript checks passed. Web and Tauri frontend production builds passed. New/imported frontend tests and helpers passed Biome; new backend tests passed Ruff.
- Final combined-checkout frontend regression run: 56 of 57 test files passed. `playback-output.test.ts` has four failures, reproduced unchanged in the original checkout; its implementation and tests were preserved. Six existing files and additional tests changed independently during validation; those edits were preserved, and the final checks ran against the combined checkout.
- No model downloads, live model evaluations, native package builds, Windows runtime, or real desktop startup acceptance were performed. Frontend production builds do not qualify sidecar packaging.

## Reviewed but excluded from this integration

- #1211: this fork already implements content/transcript/backend/model-aware multi-sample caching, off-loop preparation, atomic audio writes and concurrent-request reuse. Existing cache tests pass; retain the fork's implementation.
- #1082 and #1078: changes overlap active chunking, Chatterbox delivery and runaway-handling work. They need combined backend/model validation before adoption.
- #1061: a shared stream remains useful, but the proposed stream has no catch-up snapshot/replay for statuses missed while disconnected and no server-store subscription to replace a still-open connection when the server URL changes. It also conflicts with current code. Defer the migration.
- #1068: TADA aligner selection changes model loading, shared encoder state and prompt cache identity; defer pending real multilingual TADA validation.
- #829: thread-local progress ownership needs validation against Hugging Face worker-thread downloads, beyond its direct-thread fixture.
- #1205 and #1183: packaging-only fixes need the relevant frozen Qwen/CUDA build environments. No packaging changes were included.
- Larger playback, Studio and engine features from the queue remain separate work; existing in-progress UI/audio changes were preserved.

Raw upstream responses, the baseline hashes, integration-only patch, and check logs were retained outside Git in the integration run reports. No raw evidence is committed to the repository.

## Playback repair and publish validation — 2026-10-29

The four previously failing playback tests were reproduced and fixed. Voice sample previews now share a device-rate output context, wait for resume before advancing the clip, cancel pending starts on pause/disposal, and suppress stale failures. The preview hook releases audio nodes and object URLs on completion, replacement, error, cleanup and unmount.

- `bun run test:playback`: 15 passed, including the original four regressions and additional cancellation/retry/resource tests; wired into frontend CI.
- Exact publish candidate based on `origin/doublesharp` at `5bf9de6d6e26a211e9442125ce8e97bf32887bf3`: 292 frontend tests across 37 files, no failed checks and stable source hashes.
- Working checkout including unrelated in-progress work: 414 frontend tests across 62 files, no failed checks and stable source hashes.
- Publish-candidate backend suites: 53 passed. App/web/Tauri frontend typechecks and both frontend production builds passed.
- Independent playback review: ACCEPT, no concrete P1/P2 findings; independently ran all 41 focused playback/upstream tests.
- Cold first-click autoplay after a suspended output context resumes, physical audio output, packaged desktop behavior and model quality remain runtime acceptance gaps. No native/acoustic pass is claimed.

The startup regression fixture also isolates the existing published startup animation so it tests the lifecycle independently of DOM animation support. The publish commit includes only the reviewed upstream fixes, playback repair, their tests and CI commands, and this summary.
