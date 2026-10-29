import { StudioApiError } from './client';
import type { StudioProject } from './types';
export interface ReviewItem {
  id: string;
  label: string;
  checked: boolean;
}
export interface CheckpointReview {
  revision_id: string;
  version: number;
  note: string;
  checklist: ReviewItem[];
  updated_at: string | null;
}
export interface ReviewRequest {
  operation_id: string;
  expected_version: number;
  note: string;
  checklist: ReviewItem[];
}
export interface ReviewResponse {
  review: CheckpointReview;
  project?: StudioProject;
}
export interface ReviewTransport {
  baseUrl: string;
  get(project: string, revision: string): Promise<ReviewResponse>;
  save(project: string, revision: string, body: ReviewRequest): Promise<ReviewResponse>;
  approve(
    project: string,
    revision: string,
    body: Pick<ReviewRequest, 'operation_id' | 'expected_version'>,
  ): Promise<ReviewResponse>;
}
export function isReviewContent(
  value: unknown,
): value is Pick<CheckpointReview, 'note' | 'checklist'> {
  if (!value || typeof value !== 'object' || !('note' in value) || !('checklist' in value))
    return false;
  if (
    typeof value.note !== 'string' ||
    value.note.length > 4000 ||
    !Array.isArray(value.checklist) ||
    value.checklist.length > 12
  )
    return false;
  const ids = new Set<string>();
  return value.checklist.every((item: unknown) => {
    if (
      !item ||
      typeof item !== 'object' ||
      !('id' in item) ||
      !('label' in item) ||
      !('checked' in item)
    )
      return false;
    if (
      typeof item.id !== 'string' ||
      !item.id.length ||
      item.id.length > 80 ||
      ids.has(item.id) ||
      typeof item.label !== 'string' ||
      !item.label.length ||
      item.label.length > 120 ||
      typeof item.checked !== 'boolean'
    )
      return false;
    ids.add(item.id);
    return true;
  });
}
export class ReviewClient implements ReviewTransport {
  readonly baseUrl: string;
  constructor(
    baseUrl: string,
    private fetcher: typeof fetch = fetch,
  ) {
    this.baseUrl = baseUrl.replace(/\/+$/, '');
  }
  private async request(
    project: string,
    revision: string,
    body?: unknown,
    approval = false,
  ): Promise<ReviewResponse> {
    const fetcher = this.fetcher;
    const response = await fetcher(
      `${this.baseUrl}/studio/v1/projects/${encodeURIComponent(project)}/reviews/${encodeURIComponent(revision)}${approval ? '/approvals' : ''}`,
      body === undefined
        ? {}
        : {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              ...(approval ? { 'X-Studio-Projection': 'compact' } : {}),
            },
            body: JSON.stringify(body),
          },
    );
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new StudioApiError(
        typeof data.detail === 'string'
          ? data.detail
          : (data.detail?.message ?? `Review request failed (${response.status})`),
        response.status,
        data.detail?.current_revision,
      );
    }
    const result = (await response.json()) as ReviewResponse;
    if (
      !result?.review ||
      !isReviewContent(result.review) ||
      result.review.version < 0 ||
      (result.review.version === 0
        ? result.review.updated_at !== null
        : typeof result.review.updated_at !== 'string' || !result.review.updated_at.trim()) ||
      result.review.revision_id !== revision ||
      !Number.isInteger(result.review.version)
    )
      throw new Error('Invalid review response. Your recovery copy is retained.');
    return result;
  }
  get(project: string, revision: string) {
    return this.request(project, revision);
  }
  async save(project: string, revision: string, body: ReviewRequest) {
    const result = await this.request(project, revision, body);
    if (
      result.review.version !== body.expected_version + 1 ||
      result.review.note !== body.note ||
      result.review.checklist.length !== body.checklist.length ||
      result.review.checklist.some((entry, index) => {
        const submitted = body.checklist[index];
        return (
          entry.id !== submitted.id ||
          entry.label !== submitted.label ||
          entry.checked !== submitted.checked
        );
      })
    )
      throw new Error(
        'Review acknowledgement does not match the submitted review. Recovery retained.',
      );
    return result;
  }
  async approve(
    project: string,
    revision: string,
    body: Pick<ReviewRequest, 'operation_id' | 'expected_version'>,
  ) {
    const result = await this.request(project, revision, body, true);
    if (result.review.version !== body.expected_version)
      throw new Error(
        'Approval acknowledgement does not match the saved review version. Recovery retained.',
      );
    return result;
  }
}
