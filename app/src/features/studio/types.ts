export interface StudioDirection {
  instruct: string;
  seed: number | null;
  max_chunk_chars: number;
  crossfade_ms: number;
}
export interface StudioPassage {
  id: string;
  text: string;
  language: string;
  profile_id: string | null;
  voice_snapshot: Record<string, unknown> | null;
  direction: StudioDirection;
  input_revision: number;
  selection_token: number;
  selected_take_id: string | null;
  locked: boolean;
}
export interface StudioTrack {
  id: string;
  name: string;
  kind: 'voice' | 'music' | 'sfx';
  mute: boolean;
  solo: boolean;
  gain: number;
}
export interface StudioClip {
  id: string;
  passage_id: string | null;
  take_id: string | null;
  asset_id: string;
  track_id: string;
  start_sample: number;
  trim_start_sample: number;
  trim_end_sample: number;
  gain: number;
  fade_in_samples: number;
  fade_out_samples: number;
}
export interface StudioDocument {
  schema_version: 1;
  sample_rate: 48000;
  channels: 2;
  language: string;
  passages: StudioPassage[];
  tracks: StudioTrack[];
  clips: StudioClip[];
  mix: { target_lufs: number; max_true_peak_dbtp: number; normalize: boolean };
}
export interface StudioAsset {
  id: string;
  hash: string;
  sample_rate: number;
  channels: number;
  frames: number;
  duration: number;
  size_bytes: number;
}
export interface StudioTake {
  id: string;
  passage_id: string;
  asset_id: string;
  text: string;
  engine: string;
  model_size: string | null;
  created_at: string;
  request: Record<string, unknown>;
}
export interface StudioJob {
  id: string;
  kind: string;
  status: string;
  passage_id?: string | null;
  error?: string | null;
  result?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}
export interface StudioRevision {
  id: string;
  name: string;
  created_at: string;
  document: StudioDocument;
  project_revision: number;
}
export interface LocalApproval {
  id: string;
  revision_id: string;
  note?: string | null;
  created_at: string;
}
export interface StudioDeliverable {
  id: string;
  revision_id: string;
  kind: string;
  format: string;
  filename?: string;
  measurements?: Record<string, unknown> | null;
  created_at?: string;
}
export interface StudioProjectSummary {
  id: string;
  name: string;
  client_label: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
}
export interface StudioProject extends StudioProjectSummary {
  document: StudioDocument;
  takes: StudioTake[];
  assets: StudioAsset[];
  jobs: StudioJob[];
  revisions: StudioRevision[];
  approvals: LocalApproval[];
  deliverables: StudioDeliverable[];
}
export interface StudioEngine {
  id: string;
  name: string;
  model_size?: string;
  languages: string[];
  supports_instruct: boolean;
  supports_seed: boolean;
  expressive_tags: string[];
  ready: boolean;
  commercial_use: 'allowed' | 'restricted' | 'unknown';
}
export type StudioCommandType =
  | 'update_project'
  | 'add_passages'
  | 'update_passage'
  | 'assign_voice'
  | 'delete_passage'
  | 'reorder_passages'
  | 'select_take'
  | 'set_lock'
  | 'insert_passages'
  | 'insert_asset'
  | 'update_clip'
  | 'split_clip'
  | 'duplicate_clip'
  | 'delete_clip'
  | 'update_track'
  | 'update_mix'
  | 'undo'
  | 'redo'
  | 'restore_revision';
export interface StudioCommand {
  operation_id: string;
  expected_revision: number;
  type: StudioCommandType;
  payload: Record<string, unknown>;
}
export type PassageEdit = Partial<Pick<StudioPassage, 'text' | 'language' | 'direction'>>;
export interface StudioTransport {
  readonly baseUrl: string;
  project(id: string, signal?: AbortSignal): Promise<StudioProject>;
  command(id: string, command: StudioCommand): Promise<StudioProject>;
  checkpoint(
    id: string,
    input: { operation_id: string; expected_revision: number; name: string },
  ): Promise<StudioProject>;
  approve(
    id: string,
    input: { operation_id: string; revision_id: string; note?: string },
  ): Promise<StudioProject>;
}
