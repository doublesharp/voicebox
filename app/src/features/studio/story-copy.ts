import type { RecoveryStorage } from './session';
import { storageKeys } from './session';

interface CopyIntent {
  version: 1;
  endpoint: string;
  story_id: string;
  idempotency_key: string;
  project_id?: string;
}

export const storyCopyStorage: RecoveryStorage = {
  get length() {
    return localStorage.length;
  },
  key: (index) => localStorage.key(index),
  getItem: (key) => localStorage.getItem(key),
  setItem: (key, value) => localStorage.setItem(key, value),
  removeItem: (key) => localStorage.removeItem(key),
};

/** One persisted intent survives a lost response, remount or failed navigation. */
export class StoryCopy {
  private readonly endpoint: string;
  private readonly prefix: string;
  private busy = false;

  constructor(
    private readonly storage: RecoveryStorage,
    endpoint: string,
    private readonly storyId: string,
    private readonly fetcher: typeof fetch = fetch,
  ) {
    this.endpoint = endpoint.replace(/\/+$/, '');
    this.prefix = `voicebox-studio:story-copy:${encodeURIComponent(this.endpoint)}:${encodeURIComponent(storyId)}:`;
  }

  pending(): CopyIntent | null {
    const key = storageKeys(this.storage)
      .filter((candidate) => candidate.startsWith(this.prefix))
      .sort()[0];
    if (!key) return null;
    const raw = this.storage.getItem(key);
    if (!raw) throw new Error('The pending Story copy changed. Reopen this Story to reconcile it.');
    const record = JSON.parse(raw) as CopyIntent;
    if (
      !record ||
      record.version !== 1 ||
      record.endpoint !== this.endpoint ||
      record.story_id !== this.storyId ||
      typeof record.idempotency_key !== 'string' ||
      !record.idempotency_key.trim() ||
      record.idempotency_key.length > 200 ||
      key !== this.prefix + record.idempotency_key ||
      (record.project_id !== undefined &&
        (typeof record.project_id !== 'string' || !record.project_id.trim()))
    )
      throw new Error('Unreadable pending Story copy. Preserve local storage before recovery.');
    return record;
  }

  private persist(record: CopyIntent) {
    const raw = JSON.stringify(record);
    const key = this.prefix + record.idempotency_key;
    this.storage.setItem(key, raw);
    if (this.storage.getItem(key) !== raw)
      throw new Error('Could not safely retain the Story copy request.');
  }

  async copy(expectedKey: string | null): Promise<string> {
    if (this.busy) throw new Error('This Story copy is already being reconciled.');
    this.busy = true;
    try {
      const previous = this.pending();
      if ((previous?.idempotency_key ?? null) !== expectedKey)
        throw new Error('The pending Story copy changed. Review it before continuing.');
      if (previous?.project_id) return previous.project_id;
      const record: CopyIntent = previous ?? {
        version: 1,
        endpoint: this.endpoint,
        story_id: this.storyId,
        idempotency_key: crypto.randomUUID(),
      };
      // No mutation is sent until its original identity is durably readable.
      this.persist(record);
      const fetcher = this.fetcher;
      const response = await fetcher(
        `${this.endpoint}/studio/v1/imports/story/${encodeURIComponent(this.storyId)}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ idempotency_key: record.idempotency_key }),
        },
      );
      const body = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = body?.detail;
        throw new Error(
          typeof detail === 'string'
            ? detail
            : typeof detail?.message === 'string'
              ? detail.message
              : `Story copy failed (${response.status}). Retry the same request.`,
        );
      }
      if (
        !body ||
        typeof body.id !== 'string' ||
        !body.id.trim() ||
        typeof body.name !== 'string' ||
        body.document?.schema_version !== 1 ||
        !Array.isArray(body.document.passages) ||
        !Array.isArray(body.document.tracks) ||
        !Array.isArray(body.document.clips)
      )
        throw new Error('The Story copy acknowledgement was incomplete. Retry the same request.');
      // Retain the destination until navigation succeeds. A failed navigation opens
      // this acknowledged copy on retry, rather than creating another project.
      this.persist({ ...record, project_id: body.id });
      return body.id;
    } finally {
      this.busy = false;
    }
  }

  opened(projectId: string) {
    const record = this.pending();
    if (!record || record.project_id !== projectId)
      throw new Error('The acknowledged Story copy changed. Reopen the Story before continuing.');
    const key = this.prefix + record.idempotency_key;
    this.storage.removeItem(key);
    if (this.storage.getItem(key) !== null)
      throw new Error('Could not clear the opened Story copy receipt.');
  }
}
