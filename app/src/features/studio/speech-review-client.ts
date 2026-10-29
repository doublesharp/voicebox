import { StudioApiError } from './client';

export interface RecognizedWord {
  word: string;
  start: number;
  end: number;
}
export interface SpeechDifference {
  kind: 'omission' | 'substitution' | 'repetition' | 'extra';
  expected: string;
  recognized: string;
  expected_start: number;
  expected_end: number;
  recognized_start: number;
  recognized_end: number;
  audition: { start: number; end: number } | null;
}
export interface SpeechReview {
  version: 1;
  advisory: true;
  source_key: string;
  source: {
    asset_hash: string;
    take_text: string;
    take_engine: string;
    take_model: string | null;
    request_sha256: string;
    script_text: string;
    language: string;
    duration: number;
  };
  recognized_text: string;
  words: RecognizedWord[];
  differences: SpeechDifference[];
  recognizer: Record<string, string | null>;
}

export class SpeechReviewClient {
  constructor(
    private readonly endpoint: string,
    private readonly fetcher: typeof fetch = fetch,
  ) {}

  async recognize(
    projectId: string,
    takeId: string,
    revision: number,
    signal?: AbortSignal,
  ): Promise<SpeechReview> {
    const fetcher = this.fetcher;
    const response = await fetcher(
      `${this.endpoint.replace(/\/+$/, '')}/studio/v1/projects/${encodeURIComponent(projectId)}/speech-review`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ take_id: takeId, expected_revision: revision }),
        signal,
      },
    );
    const value = await response.json();
    if (!response.ok)
      throw new StudioApiError(
        typeof value?.detail === 'string'
          ? value.detail
          : `Speech review failed (${response.status})`,
        response.status,
      );
    if (
      !value ||
      value.version !== 1 ||
      value.advisory !== true ||
      !/^[0-9a-f]{64}$/.test(value.source_key) ||
      typeof value.source?.asset_hash !== 'string' ||
      typeof value.recognized_text !== 'string' ||
      !Array.isArray(value.words) ||
      value.words.length > 2000 ||
      !Array.isArray(value.differences) ||
      value.words.some(
        (word: RecognizedWord) =>
          !word ||
          typeof word.word !== 'string' ||
          !Number.isFinite(word.start) ||
          !Number.isFinite(word.end) ||
          word.start < 0 ||
          word.end <= word.start,
      ) ||
      value.differences.some(
        (item: SpeechDifference) =>
          !item ||
          !['omission', 'substitution', 'repetition', 'extra'].includes(item.kind) ||
          typeof item.expected !== 'string' ||
          typeof item.recognized !== 'string' ||
          (item.audition !== null &&
            (!Number.isFinite(item.audition?.start) ||
              !Number.isFinite(item.audition?.end) ||
              item.audition.start < 0 ||
              item.audition.end <= item.audition.start)),
      )
    ) {
      throw new Error('Invalid recognized speech report. Compare the take by listening.');
    }
    return value as SpeechReview;
  }
}
