import type {
  AuditionLevel,
  StudioAsset,
  StudioCommand,
  StudioEngine,
  StudioJob,
  StudioProject,
  StudioProjectSummary,
  StudioTransport,
} from './types';

export interface StudioProfile {
  id: string;
  name: string;
  language: string;
  voice_type: string;
  preset_engine?: string;
  default_engine?: string;
}
export interface LegacyStory {
  id: string;
  name: string;
}
export class StudioApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly currentRevision?: number,
  ) {
    super(message);
    this.name = 'StudioApiError';
  }
}
export class StudioClient implements StudioTransport {
  readonly baseUrl: string;
  constructor(
    baseUrl: string,
    private readonly fetcher: typeof fetch = fetch,
  ) {
    this.baseUrl = baseUrl.replace(/\/+$/, '');
  }
  url(path: string) {
    return `${this.baseUrl}/studio/v1${path}`;
  }
  private async request<T>(path: string, options: RequestInit = {}, legacy = false): Promise<T> {
    const response = await this.fetcher(
      legacy ? `${this.baseUrl}${path}` : this.url(path),
      options,
    );
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const detail = body.detail;
      const message =
        typeof detail === 'string'
          ? detail
          : typeof detail?.message === 'string'
            ? detail.message
            : Array.isArray(detail)
              ? detail
                  .map((v: { msg?: string }) => v.msg)
                  .filter(Boolean)
                  .join('; ')
              : `Request failed (${response.status})`;
      throw new StudioApiError(message, response.status, detail?.current_revision);
    }
    return response.json() as Promise<T>;
  }
  private post<T>(path: string, body: unknown) {
    return this.request<T>(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }
  projects(signal?: AbortSignal) {
    return this.request<{ items: StudioProjectSummary[] }>('/projects', { signal });
  }
  createProject(input: {
    name: string;
    client_label?: string;
    script?: string;
    language?: string;
  }) {
    return this.post<StudioProject>('/projects', input);
  }
  project(id: string, signal?: AbortSignal) {
    return this.request<StudioProject>(`/projects/${encodeURIComponent(id)}`, { signal });
  }
  command(id: string, input: StudioCommand) {
    return this.post<StudioProject>(`/projects/${encodeURIComponent(id)}/commands`, input);
  }
  checkpoint(id: string, input: { operation_id: string; expected_revision: number; name: string }) {
    return this.post<StudioProject>(`/projects/${encodeURIComponent(id)}/revisions`, input);
  }
  approve(id: string, input: { operation_id: string; revision_id: string; note?: string }) {
    return this.post<StudioProject>(`/projects/${encodeURIComponent(id)}/approvals`, input);
  }
  engines(signal?: AbortSignal) {
    return this.request<{ items: StudioEngine[] }>('/engines', { signal });
  }
  profiles(signal?: AbortSignal) {
    return this.request<StudioProfile[]>('/profiles', { signal }, true);
  }
  stories(signal?: AbortSignal) {
    return this.request<LegacyStory[]>('/stories', { signal }, true);
  }
  generate(
    id: string,
    input: {
      idempotency_key: string;
      passage_ids: string[];
      count: 1 | 2 | 3;
      adopt: boolean;
      commercial_only?: boolean;
    },
  ) {
    return this.post<{ jobs: StudioJob[] }>(
      `/projects/${encodeURIComponent(id)}/generation-jobs`,
      input,
    );
  }
  render(
    id: string,
    input: {
      idempotency_key: string;
      revision_id: string;
      kind: 'mix' | 'stems';
      format: 'wav' | 'mp3';
      processing?: 'dry' | 'processed';
      allow_stale_audio?: boolean;
    },
  ) {
    return this.post<{ job: StudioJob }>(`/projects/${encodeURIComponent(id)}/render-jobs`, input);
  }
  job(id: string) {
    return this.request<StudioJob>(`/jobs/${encodeURIComponent(id)}`);
  }
  cancelJob(id: string) {
    return this.post<StudioJob>(`/jobs/${encodeURIComponent(id)}/cancel`, {});
  }
  retryJob(id: string) {
    return this.post<StudioJob>(`/jobs/${encodeURIComponent(id)}/retry`, {});
  }
  assetAudio(id: string) {
    return this.url(`/assets/${encodeURIComponent(id)}/audio`);
  }
  auditionLevel(id: string, signal?: AbortSignal) {
    return this.request<AuditionLevel>(`/assets/${encodeURIComponent(id)}/audition-level`, {
      signal,
    });
  }
  deliverableAudio(id: string) {
    return this.url(`/deliverables/${encodeURIComponent(id)}/audio`);
  }
  deliverableDownload(id: string) {
    return this.url(`/deliverables/${encodeURIComponent(id)}/download`);
  }
  archiveUrl(id: string) {
    return this.url(`/projects/${encodeURIComponent(id)}/archive`);
  }
  private upload<T>(path: string, file: File) {
    const body = new FormData();
    body.append('file', file);
    return this.request<T>(path, { method: 'POST', body });
  }
  importArchive(file: File) {
    return this.upload<StudioProject>('/imports/archive', file);
  }
  importStory(id: string) {
    return this.post<StudioProject>(`/imports/story/${encodeURIComponent(id)}`, {});
  }
  uploadAsset(id: string, file: File) {
    return this.upload<StudioAsset>(`/projects/${encodeURIComponent(id)}/assets`, file);
  }
  async download(url: string) {
    const response = await this.fetcher(url);
    if (!response.ok)
      throw new StudioApiError(`Download failed (${response.status})`, response.status);
    return response.blob();
  }
}
