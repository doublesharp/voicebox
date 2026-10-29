# Studio S1 implementation contract

Date: 2026-10-28. Implements [the specification](PRO_STUDIO_SPEC.md) after the user's explicit implementation request. Coordinate project `project_14a676bd-5cff-4ffe-ad9c-9aa08abc554e` is the authoritative task/artifact/ownership record; this file is the shared source interface view.

## Parallel ownership

Current task-specific file ownership and parallel workstreams live in local Coordinate. Preparation tasks own their assigned feature files; only root integrates and pushes Git branches. Reserving the same integration target for multiple preparation tasks unnecessarily serializes them. Root owns backend registration, central contract changes, cross-domain checks and the final merge.

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
  take_annotations?: Record<string, {name: string; note: string}>;
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
| select_take | `{passage_id, take_id, replace_clips?: boolean, reset_trims?:boolean}`; explicit reset requires replacement, preserves starts/gains, and clears source trims and any fades that cannot fit |
| annotate_take | `{take_id, name?, note?}`; at least one annotation field, name up to 120 characters and note up to 4000; ordinary revision/idempotency/undo semantics |
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

`GET /engines?commercial_only=false` returns `{items: StudioEngine[]}`. Each engine advertises `id`, `name`, `model_size`, `languages`, `supports_instruct`, `supports_seed`, `supports_voice_design`, `expressive_tags`, `recognized_tags`, `supported_attributes`, `attribute_vocabulary`, `limits`, `ready`, `commercial_use`, `registry_identity` and `registry_revision`. `streaming.qualified` and `performance_conversion.qualified` are explicit qualification flags, not inferred from an adapter signature.

`registry_identity` records the selected engine/model repositories, actual per-engine adapter and available source hash, relevant runtime/engine package versions, platform and Python identity. Unknown weights revision or unavailable bundled Python source remains `null`. `registry_revision` hashes this identity and declared semantics; it is not a claim that model weights or native execution have been qualified. Base Qwen delivery instructions remain unsupported; CustomVoice/VoiceDesign declarations follow their own configuration. Commercial filtering admits only `commercial_use:'allowed'`; restricted and unknown terms are excluded and the filtered list may be empty. The UI consumes backend tag vocabulary rather than maintaining an independent emotion list.

Generation: `POST /projects/{id}/generation-jobs` accepts `{idempotency_key, passage_ids: string[], count: 1|2|3, adopt: boolean, commercial_only?:boolean}` and returns `202 {jobs: StudioJob[]}`. Validate every passage before committing any jobs. Default/false commercial mode preserves the previous request replay identity; true mode requires every model's commercial suitability to be allowed. Snapshot capabilities and effective direction before queueing, retain inactive direction in the saved passage, and exclude unsupported instruction/seed settings from synthesis. Recognized unsupported expressive tokens require an actionable resolution; ordinary bracketed prose is not automatically treated as a stage tag. Inputs never silently re-read mutable voice defaults. Adoption still compares passage/selection intent.

`GET /jobs/{id}`, `POST /jobs/{id}/cancel`, and `POST /jobs/{id}/retry` expose durable status. Both the job reader and project job arrays include `phase` and `attempts` with ordered phase events and errors. Generation phases include loading_model, generating and processing; injected custom adapters do not fabricate model-loading phases. Retry retains the logical job and records a new attempt. Takes record `creation_job_id`, `creation_attempt`, capability/runtime identity and effective direction in their frozen request. Render jobs retain generic status until render lifecycle instrumentation is implemented.

Take minimum fields: `{id, passage_id, asset_id, text, engine, model_size, created_at, request}`. Asset minimum fields: `{id, hash, sample_rate, channels, frames, duration, size_bytes}`. Job minimum fields: `{id, kind, status, passage_id?, error?, result?, created_at, updated_at}`. Asset audio: `GET /assets/{id}/audio`.

Render: `POST /projects/{id}/render-jobs` accepts `{idempotency_key, revision_id, kind:'mix'|'stems', format:'wav'|'mp3', allow_stale_audio?:boolean}` and returns `202 {job}`. Preflight against the frozen revision; assets must be complete. Job result contains managed deliverables. `GET /deliverables/{id}/audio` auditions the actual encoded file; `GET /deliverables/{id}/download` downloads it. Download publication and native destination publication remain distinct.

Every narration passage needs a selected take and audible pinned timeline audio. Explicit `allow_stale_audio` records an immutable `recording_acknowledgements` entry bound to the revision and actual clip/take/text. Deliverables carry recorded/script text, acknowledgement, source revision, filename, and actual-file measurements. Dry stems retain relative track levels and bypass master normalization.

Preview: `GET /assets/{id}/preview?start_sample=N&frames=N` returns only the requested range as a standalone 48 kHz WAV; the maximum range is 240000 samples. Timeline playback reads sequential two-second windows with bounded lookahead and fails visibly on underrun. `GET /assets/{id}/waveform?bins=512` returns native `{sample_rate,frames,channels,peaks:[[min,max],...]}`; bins range from 32 to 2048. Peaks are read in bounded blocks and cached by immutable source hash with a 64 MiB disk limit. Waveforms are source-derived; live meters report sample peaks, not LUFS or true peak.

Take comparison: `GET /assets/{id}/audition-level` returns `{method:'sample_rms_v1',status:'measured'|'silence',rms,peak,frames,measured_frames,sample_rate,channels}`. It samples at most five native windows with a 240000-frame total ceiling and bounded reads, cached by source hash/method with a 4 MiB disk limit. This is a sampled listening estimate, not integrated LUFS or full-file true peak. Matching only attenuates temporary audition, never sources, arrangement gain or delivery mastering. Silence, incompatible layouts and unavailable measurements remain explicit; ordinary audition stays available.

Annotation keystrokes enter the same session recovery/conflict boundary as script edits. Journal version 2 includes `takeDrafts`; version 1 remains readable. Inspector navigation preserves drafts, polling cannot rebase their original revision, and acknowledgements preserve newer typing. Older clients treat version 2 as an unreadable/exportable recovery record rather than silently dropping its annotation data.

Archives: `GET /projects/{id}/archive` returns a complete managed ZIP, staged/validated before streaming. `POST /imports/archive` accepts multipart `file` and returns a new complete project. `POST /imports/story/{story_id}` creates a studio-owned copy, preserving legacy data. `POST /projects/{id}/assets` accepts multipart audio `file`; returned metadata can be placed with `insert_asset`. Annotation maps are validated against project takes; imports remap only their take-ID keys and preserve note text.

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


## Checkpoint reviews and native publication

`GET /studio/v1/projects/{project_id}/reviews/{revision_id}` returns `{review}`. An untouched checkpoint has version 0, empty note/checklist and null update time. `POST` accepts `operation_id`, `expected_version`, `note` and ordered `checklist` entries `{id,label,checked}`. Saving uses independent version CAS and exact request replay. `POST .../approvals` accepts the operation and acknowledged review version, atomically freezes the saved note/checklist into a local approval, and returns `{review,project}`. Changed replay content conflicts. Approval of an unsaved version 0 is rejected.

Review keystrokes and uncertain save/approval identities are journaled per session and checkpoint, preserving original versions through refresh and restart. Unacknowledged newer typing survives acknowledgement; explicit recovery never consumes another window's journal. Checklist comparison ignores JSON object key order while preserving entry order and values. Archives include only persisted reviews for included checkpoints, remap identity fields only, and restore them in the same import transaction.

Platform saves return `saved`, `cancelled`, or browser `download_started`; legacy void adapters remain compatible. Native save commands own the destination chooser, stream URL downloads into an exclusive sibling staging file, flush/sync before rename, and preserve existing Unix file permissions. The URL bridge avoids loading long downloads into JavaScript. These source and filesystem guarantees do not establish dialog, Windows ACL/replacement or packaged-runtime qualification.


## Studio mixing and portable workflows

Render requests optionally specify `processing: "dry" | "processed"`. Absent processing retains the old request identity and defaults to a processed mix or dry stems. Dry output retains arrangement, source/track gain, trims and fades. Processed tracks use validated registry effects with persistent block state and a bounded tail; processed stems bypass all master processing and retain relative levels. Enabled pitch shifting is rejected by Studio until variable plugin output can be aligned; the legacy Effects workflow retains it. Studio parameter gestures commit on release; saving disables mutation controls explicitly without unmounting their editor.

A processed mix may apply master gain, loudness normalization and a four-times oversampled limiter at the configured peak target. The final encoded file is measured when FFmpeg is available. Limiting is not certified for final true-peak compliance after resampling or encoding. Silent or insufficient measurements remain unavailable. Render attempts expose mixing, encoding, metering and publishing phases, record the frozen recipe and source/output hashes, and preserve registered output when commit acknowledgement is uncertain.

`POST /studio/v1/imports/story/{story_id}` accepts an optional strict `idempotency_key`; omitted bodies retain the independent-copy legacy behavior. A key replays the exact original committed response without rereading changed/deleted Story data. Copying retains pinned owned audio, placement, overlap, trim and gain. Unknown imported voices are conservatively separated by original track and pinned generation/version, never inferred equal across unrelated recordings. Track routing and stem preflight share frozen identity semantics.

`GET .../projects/{project_id}/archive` accepts `take_scope=all|used` and `include_regeneration_references=true|false`. Defaults retain all available owned takes/assets. Explicit manifest scope describes omitted alternatives/references, retained checkpoints, excluded counts, absent undo history, and provenance-only job history. Used-take exports keep selected and clipped takes across all included checkpoints and frozen documents; saved approvals/reviews remain included. The importer restores playback even when only schema-known cloned-voice references are unavailable, preserves explicit missing-reference markers through re-export, and blocks regeneration until reassignment. Required audio, unknown missing references and present corrupt assets still fail validation. The export UI flushes the draft and requires acknowledgement; review journals remain separate from saved review records.
