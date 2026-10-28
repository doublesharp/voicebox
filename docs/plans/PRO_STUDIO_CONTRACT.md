# Studio S1 implementation contract

Date: 2026-10-28. Implements [the specification](PRO_STUDIO_SPEC.md) after the user's explicit implementation request. Coordinate project `project_14a676bd-5cff-4ffe-ad9c-9aa08abc554e` is the authoritative task/artifact/ownership record; this file is the shared source interface view.

## Parallel ownership

- Storage/API/generation worker: `backend/studio/` except `rendering.py` and `archive.py`; `backend/tests/test_studio_storage.py`, `test_studio_jobs.py`, `test_studio_api.py`; narrowly scoped shared generation-arbitration integration.
- Frontend worker: `app/src/features/studio/`, `app/tests/studio*.test.*`, and studio route/navigation integration in `app/src/router.tsx` and `app/src/components/Sidebar.tsx`.
- Rendering/archive worker: `backend/studio/rendering.py`, `backend/studio/archive.py`, `backend/tests/test_studio_rendering.py`, `backend/tests/test_studio_archive.py` only.
- Root: backend registration/lifecycle integration, cross-domain integration checks, source/build qualification, documentation and Coordinate status. Root resolves cross-boundary defects.

Workers share this checkout with strict ownership to preserve the existing uncommitted branding and playback work. No worker resets, checks out, commits, stages, or rewrites another owner's files. Dependencies, model caches, and native identities remain unchanged. No new framework or package installation is planned.

## Transport

Base URL comes from the existing server store. New requests use `/studio/v1`; legacy APIs stay compatible. JSON uses snake_case. Every mutation receives `operation_id` or `idempotency_key` as appropriate. Revision conflicts are `409` with `detail` containing `message` and `current_revision`; validation failures are `422`.

`StudioProject` response:

```ts
{
  id: string; name: string; client_label: string | null;
  revision: number; created_at: string; updated_at: string;
  document: StudioDocument;
  takes: StudioTake[]; assets: StudioAsset[]; jobs: StudioJob[];
  revisions: StudioRevision[]; approvals: LocalApproval[];
  deliverables: StudioDeliverable[];
}
```

`GET /projects` returns `{items: StudioProjectSummary[]}`. A summary is the project response without the document and related arrays. `POST /projects` accepts `{name, client_label?, script?, language?}` and returns a complete project. Pasted script splits at blank paragraphs, preserving nonempty text. `GET /projects/{id}` returns a complete project.

Document:

```ts
{
  schema_version: 1; sample_rate: 48000; channels: 2;
  language: string;
  passages: Array<{
    id: string; text: string; language: string;
    profile_id: string | null; voice_snapshot: Record<string, unknown> | null;
    direction: {instruct: string; seed: number | null; max_chunk_chars: number; crossfade_ms: number};
    input_revision: number; selection_token: number;
    selected_take_id: string | null; locked: boolean;
  }>;
  tracks: Array<{id: string; name: string; kind: 'voice'|'music'|'sfx'; mute: boolean; solo: boolean; gain: number}>;
  clips: Array<{
    id: string; passage_id: string | null; take_id: string | null;
    asset_id: string; track_id: string; start_sample: number;
    trim_start_sample: number; trim_end_sample: number;
    gain: number; fade_in_samples: number; fade_out_samples: number;
  }>;
  mix: {target_lufs: number; max_true_peak_dbtp: number; normalize: boolean};
}
```

Create one default voice track. Clip placement and trims are integer project-rate samples; trim_end_sample is the number removed from the resampled source's end. Gains are linear. Source assets retain their native sample rate/channels/frames. Runtime validators must reject nonfinite values, negative placement/trims/fades, impossible source ranges and cross-project references.

All nonnegative time/frame conversions use nearest-sample rounding, with exact half samples rounded toward positive infinity, matching JavaScript `Math.round`. Backend conversions share `studio/timing.py`.

S1 documents and imported checkpoints require a 48 kHz stereo working format. Native source assets may retain different sample rates and mono/stereo channels. Unsupported project working formats are rejected before import publication.

`POST /projects/{id}/commands` accepts `{operation_id, expected_revision, type, payload}` and returns a complete project. A repeated identical operation ID returns its original committed result with explicit revision semantics; a different payload with the same ID conflicts. Required commands:

| Type | Payload |
| --- | --- |
| update_project | `{name?, client_label?}` |
| add_passages | `{texts: string[], after_id?: string}` |
| update_passage | `{passage_id, text?, language?, direction?}` |
| assign_voice | `{passage_ids: string[], profile_id: string, model_size?:string}`; backend validates and freezes the exact compatible variant and copied references |
| delete_passage | `{passage_id}`; remove related clips through one command, retain takes/assets/history |
| reorder_passages | `{passage_ids: string[]}`; exact existing passage set |
| select_take | `{passage_id, take_id, replace_clips?: boolean}` |
| set_lock | `{passage_id, locked: boolean}` |
| insert_passages | `{passage_ids: string[], start_sample?: number, gap_ms?: number}`; append by default |
| insert_asset | `{asset_id, start_sample?: number, track_id?: string, kind?: 'music'|'sfx'|'voice', name?:string}`; create a track when absent; undo retains the immutable source |
| update_clip | `{clip_id, start_sample?, trim_start_sample?, trim_end_sample?, gain?, fade_in_samples?, fade_out_samples?, track_id?}` |
| split_clip | `{clip_id, at_sample}`; absolute timeline position |
| duplicate_clip / delete_clip | `{clip_id}` |
| update_track | `{track_id, name?, mute?, solo?, gain?}` |
| update_mix | `{target_lufs?, max_true_peak_dbtp?, normalize?}` |
| undo / redo | `{}` |
| restore_revision | `{revision_id}`; new draft, existing checkpoint unchanged |

Checkpoints: `POST /projects/{id}/revisions` accepts `{operation_id, expected_revision, name}`. Local approval: `POST /projects/{id}/approvals` accepts `{operation_id, revision_id, note?}`. Both return a complete project. Immutable revisions contain `{id, name, created_at, document, project_revision}`.

`GET /engines` returns `{items: Array<{id, name, model_size?, languages: string[], supports_instruct: boolean, supports_seed: boolean, expressive_tags: string[], ready: boolean, commercial_use: 'allowed'|'restricted'|'unknown'}>}`. Use actual registry/platform capabilities; no universal emotion sliders or automatic model downloads.

Generation: `POST /projects/{id}/generation-jobs` accepts `{idempotency_key, passage_ids: string[], count: 1|2|3, adopt: boolean}` and returns `202 {jobs: StudioJob[]}`. Snapshot inputs at submission, persist before response, compare passage/selection intent before adoption. `GET /jobs/{id}`, `POST /jobs/{id}/cancel`, and `POST /jobs/{id}/retry` expose durable status. `GET /projects/{id}` includes recent jobs and completed takes, supporting polling without SSE.

Take minimum fields: `{id, passage_id, asset_id, text, engine, model_size, created_at, request}`. Asset minimum fields: `{id, hash, sample_rate, channels, frames, duration, size_bytes}`. Job minimum fields: `{id, kind, status, passage_id?, error?, result?, created_at, updated_at}`. Asset audio: `GET /assets/{id}/audio`.

Render: `POST /projects/{id}/render-jobs` accepts `{idempotency_key, revision_id, kind:'mix'|'stems', format:'wav'|'mp3', allow_stale_audio?:boolean}` and returns `202 {job}`. Preflight against the frozen revision; assets must be complete. Job result contains managed deliverables. `GET /deliverables/{id}/audio` auditions the actual encoded file; `GET /deliverables/{id}/download` downloads it. Download publication and native destination publication remain distinct.

Every narration passage needs a selected take and audible pinned timeline audio. Explicit `allow_stale_audio` records an immutable `recording_acknowledgements` entry bound to the revision and actual clip/take/text. Deliverables carry recorded/script text, acknowledgement, source revision, filename, and actual-file measurements. Dry stems retain relative track levels and bypass master normalization.

Preview: `GET /assets/{id}/preview?start_sample=N&frames=N` returns only the requested range as a standalone 48 kHz WAV; the maximum range is 240000 samples. Timeline playback reads sequential two-second windows with bounded lookahead and fails visibly on underrun. `GET /assets/{id}/waveform?bins=512` returns native `{sample_rate,frames,channels,peaks:[[min,max],...]}`; bins range from 32 to 2048. Peaks are read in bounded blocks and cached by immutable source hash with a 64 MiB disk limit. Waveforms are source-derived; live meters report sample peaks, not LUFS or true peak.

Archives: `GET /projects/{id}/archive` returns a complete managed ZIP, staged/validated before streaming. `POST /imports/archive` accepts multipart `file` and returns a new complete project. `POST /imports/story/{story_id}` creates a studio-owned copy, preserving legacy data. `POST /projects/{id}/assets` accepts multipart audio `file`; returns asset metadata, allowing a later clip insertion command when implemented.

## Pure rendering and archive module interfaces

Rendering worker exports:

```python
def render_project(document: dict, assets: dict[str, dict], resolve_asset, output_path,
                   *, kind="mix", format="wav", cancelled=None, progress=None) -> dict:
    # resolve_asset(asset_id) -> pathlib.Path, output_path is a task-owned directory.
    # cancelled() is checked between bounded blocks and while encoding/metering.
    # progress(completed_frames, total_frames) reports the mixing stage.
    # kind="stems" produces dry stems without master loudness normalization.
    # Memory is block-bounded; accepted placement is limited to two hours.
    # Return {"files": [{"path": str, "kind": str, "track_id": str | None,
    #                     "sample_rate": int, "channels": int}],
    #         "measurements": dict, "warnings": list[str]}.
    ...

def write_archive(project_payload: dict, assets: list[dict], resolve_asset, output_path) -> dict:
    # output_path is a ZIP filename; return {"path": str, "size_bytes": int}.
    ...

def read_archive(archive_path, destination, *, max_total_bytes=1073741824,
                 max_files=10000) -> dict:
    # Stage all content in destination; return validated
    # {"project": dict, "assets": list[dict]} with each asset including
    # a safe staged "path". Caller remaps/imports into its own storage atomically.
    ...
```

Renderer has no store/database imports. Use existing numpy/soundfile/scipy/FFmpeg capabilities where installed. Preserve sources, fixed clip starts, trims, gains/fades, overlaps and track mute/solo. Silence is not a measurable loudness value. True peak/LUFS claims require a qualified implementation; if missing, report unavailable and do not label RMS/peak clipping as equivalent. Archive routines have no store imports and reject traversal, symlinks, unsupported schema, hash/size mismatches, oversized expansion, and missing required audio.

Core backend imports those pure modules through thin services. The initial store, metadata/API/generation implementation must remain usable while renderer/archive modules are developed in parallel. Root owns the final integration and resolves a justified contract change across workers.
