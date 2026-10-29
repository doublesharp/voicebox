import { StudioApiError } from './client';

export interface CaptionCue {
  id: string;
  clip_id: string | null;
  start_sample: number;
  end_sample: number;
  text: string;
}
export interface CaptionDocument {
  source_fingerprint: string;
  reviewed: boolean;
  cues: CaptionCue[];
}
export interface ReferenceVideo {
  name: string;
  frame_rate: number;
  time_origin_sample: number;
  duration_seconds: number | null;
}
export interface CaptionStatus {
  source_fingerprint: string;
  current: boolean;
  reviewed: boolean;
  exportable: boolean;
  captions: CaptionDocument | null;
  scene_timing: Array<{
    passage_id: string;
    target_seconds: number;
    selected_seconds: number | null;
    placed_seconds: number;
    delta_seconds: number | null;
  }>;
}
export class CaptionsClient {
  constructor(
    private readonly endpoint: string,
    private readonly fetcher: typeof fetch = fetch,
  ) {}
  exportUrl(projectId: string, format: 'srt' | 'vtt', fingerprint: string) {
    const query = new URLSearchParams({ format, source_fingerprint: fingerprint });
    return `${this.endpoint.replace(/\/+$/, '')}/studio/v1/projects/${encodeURIComponent(projectId)}/captions/export?${query}`;
  }
  async status(projectId: string, signal?: AbortSignal): Promise<CaptionStatus> {
    const fetcher = this.fetcher;
    const response = await fetcher(
      `${this.endpoint.replace(/\/+$/, '')}/studio/v1/projects/${encodeURIComponent(projectId)}/captions`,
      { signal },
    );
    const value = await response.json();
    if (!response.ok)
      throw new StudioApiError(
        typeof value?.detail === 'string'
          ? value.detail
          : `Caption status unavailable (${response.status})`,
        response.status,
      );
    if (
      !value ||
      !/^[0-9a-f]{64}$/.test(value.source_fingerprint) ||
      ['current', 'reviewed', 'exportable'].some((key) => typeof value[key] !== 'boolean') ||
      !Array.isArray(value.scene_timing)
    )
      throw new Error('Invalid caption status response. Refresh before exporting.');
    return value as CaptionStatus;
  }
}
