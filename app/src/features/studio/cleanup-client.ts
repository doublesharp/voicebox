import type { RecoveryStorage } from './session';
import { storageKeys } from './session';

export interface CleanupPreview {
  project_id: string;
  preview_token: string;
  candidates: Array<{ id: string; hash: string; size_bytes: number; duration: number }>;
  protected_count: number;
  trash: Array<{ id: string; asset_id: string; size_bytes: number; created_at: string }>;
  policy: string;
}
export type CleanupBody = {
  operation_id: string;
  preview_token: string;
  asset_ids: string[];
};
export type RestoreBody = { operation_id: string; receipt_ids: string[] };
export type CleanupAction = 'cleanup' | 'restore';
export interface CleanupReceipt {
  operation_id: string;
  project_id: string;
  action: CleanupAction;
  receipts: Array<{ id: string; asset_id: string }>;
  policy: string;
}
export interface CleanupTransport {
  baseUrl: string;
  preview(project: string): Promise<CleanupPreview>;
  apply(
    project: string,
    action: CleanupAction,
    body: CleanupBody | RestoreBody,
  ): Promise<CleanupReceipt>;
}
interface PendingCleanup {
  version: 1;
  action: CleanupAction;
  body: CleanupBody | RestoreBody;
  rejected?: boolean;
}
const id = (value: unknown): value is string =>
  typeof value === 'string' &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
const ids = (value: unknown): value is string[] =>
  Array.isArray(value) &&
  value.length >= 1 &&
  value.length <= 100 &&
  value.every(id) &&
  new Set(value).size === value.length;

function validBody(action: CleanupAction, value: unknown): value is CleanupBody | RestoreBody {
  if (
    !value ||
    typeof value !== 'object' ||
    !('operation_id' in value) ||
    typeof value.operation_id !== 'string' ||
    !value.operation_id.trim() ||
    value.operation_id.length > 200
  )
    return false;
  return action === 'cleanup'
    ? 'preview_token' in value &&
        typeof value.preview_token === 'string' &&
        /^[0-9a-f]{64}$/.test(value.preview_token) &&
        'asset_ids' in value &&
        ids(value.asset_ids)
    : action === 'restore' && 'receipt_ids' in value && ids(value.receipt_ids);
}

class CleanupError extends Error {
  constructor(
    message: string,
    readonly rejected: boolean,
  ) {
    super(message);
  }
}
export class CleanupClient implements CleanupTransport {
  readonly baseUrl: string;
  constructor(
    endpoint: string,
    private readonly fetcher: typeof fetch = fetch,
  ) {
    this.baseUrl = endpoint.replace(/\/+$/, '');
  }
  private async request(
    project: string,
    action?: CleanupAction,
    body?: CleanupBody | RestoreBody,
  ): Promise<unknown> {
    const response = await this.fetcher(
      `${this.baseUrl}/studio/v1/projects/${encodeURIComponent(project)}/storage${action ? `/${action}` : ''}`,
      body
        ? {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          }
        : undefined,
    );
    const result = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = result?.detail;
      throw new CleanupError(
        typeof detail === 'string'
          ? detail
          : (detail?.message ?? `Storage request failed (${response.status})`),
        response.status === 422 || detail?.code === 'storage_preview_changed',
      );
    }
    return result;
  }
  async preview(project: string) {
    const result = (await this.request(project)) as CleanupPreview;
    if (
      !result ||
      result.project_id !== project ||
      !/^[0-9a-f]{64}$/.test(result.preview_token) ||
      !Number.isInteger(result.protected_count) ||
      result.protected_count < 0 ||
      typeof result.policy !== 'string' ||
      !Array.isArray(result.candidates) ||
      !Array.isArray(result.trash) ||
      result.candidates.some(
        (item) =>
          !item ||
          !id(item.id) ||
          !/^[0-9a-f]{64}$/.test(item.hash) ||
          !Number.isSafeInteger(item.size_bytes) ||
          item.size_bytes <= 0 ||
          !Number.isFinite(item.duration) ||
          item.duration <= 0,
      ) ||
      result.trash.some(
        (item) =>
          !item ||
          !id(item.id) ||
          !id(item.asset_id) ||
          !Number.isSafeInteger(item.size_bytes) ||
          item.size_bytes <= 0 ||
          typeof item.created_at !== 'string',
      )
    )
      throw new Error('Invalid storage preview. Nothing was changed.');
    return result;
  }
  async apply(project: string, action: CleanupAction, body: CleanupBody | RestoreBody) {
    if (!validBody(action, body)) throw new Error('Invalid cleanup request. Nothing was sent.');
    const result = (await this.request(project, action, body)) as CleanupReceipt;
    const expected =
      action === 'cleanup' ? (body as CleanupBody).asset_ids : (body as RestoreBody).receipt_ids;
    if (
      !result ||
      result.project_id !== project ||
      result.action !== action ||
      result.operation_id !== body.operation_id ||
      typeof result.policy !== 'string' ||
      !Array.isArray(result.receipts) ||
      result.receipts.length !== expected.length ||
      new Set(result.receipts.map((item) => item?.id)).size !== expected.length ||
      result.receipts.some(
        (item, index) =>
          !item ||
          !id(item.id) ||
          !id(item.asset_id) ||
          (action === 'cleanup' ? item.asset_id : item.id) !== expected[index],
      )
    )
      throw new Error('Storage acknowledgement did not match the request. Retry the same request.');
    return result;
  }
}

export class CleanupSession {
  private readonly prefix: string;
  private active = false;
  constructor(
    private readonly storage: RecoveryStorage,
    private readonly client: CleanupTransport,
    private readonly project: string,
  ) {
    this.prefix = `voicebox-studio:cleanup:${encodeURIComponent(client.baseUrl)}:${encodeURIComponent(project)}:`;
  }
  pending(): PendingCleanup | null {
    const key = storageKeys(this.storage)
      .filter((item) => item.startsWith(this.prefix))
      .sort()[0];
    if (!key) return null;
    const raw = this.storage.getItem(key);
    if (!raw) throw new Error('Pending storage request changed. Reopen storage to reconcile it.');
    const record = JSON.parse(raw) as PendingCleanup;
    if (
      !record ||
      record.version !== 1 ||
      !['cleanup', 'restore'].includes(record.action) ||
      !validBody(record.action, record.body) ||
      key !== this.prefix + record.body.operation_id ||
      (record.rejected !== undefined && typeof record.rejected !== 'boolean')
    )
      throw new Error(
        'Unreadable pending storage request. Preserve local storage before recovery.',
      );
    return record;
  }
  private persist(record: PendingCleanup) {
    const raw = JSON.stringify(record),
      key = this.prefix + record.body.operation_id;
    this.storage.setItem(key, raw);
    if (this.storage.getItem(key) !== raw)
      throw new Error('Local storage did not retain the request. The storage action is blocked.');
    return { key, raw };
  }
  async submit(
    action: CleanupAction,
    create: () => CleanupBody | RestoreBody,
    expectedId: string | null,
  ) {
    if (this.active) throw new Error('A storage request is already running.');
    this.active = true;
    try {
      const prior = this.pending();
      if ((prior?.body.operation_id ?? null) !== expectedId)
        throw new Error('The pending storage request changed. Review it before continuing.');
      const record: PendingCleanup = prior
        ? { ...prior, rejected: false }
        : { version: 1, action, body: structuredClone(create()) };
      if (record.action !== action || !validBody(record.action, record.body))
        throw new Error('Invalid pending storage request.');
      const { key, raw } = this.persist(record);
      let receipt: CleanupReceipt;
      try {
        receipt = await this.client.apply(
          this.project,
          record.action,
          structuredClone(record.body),
        );
      } catch (error) {
        if (error instanceof CleanupError && error.rejected && this.storage.getItem(key) === raw)
          this.persist({ ...record, rejected: true });
        throw error;
      }
      if (this.storage.getItem(key) !== raw)
        throw new Error(
          'The storage journal changed before acknowledgement. Reconcile its original request.',
        );
      this.storage.removeItem(key);
      if (this.storage.getItem(key) !== null)
        throw new Error(
          'Could not clear the acknowledged storage request. Retry the same request.',
        );
      return receipt;
    } finally {
      this.active = false;
    }
  }
  discardRejected() {
    const record = this.pending();
    if (!record?.rejected)
      throw new Error('An uncertain storage request must be replayed before discarding it.');
    const key = this.prefix + record.body.operation_id,
      raw = this.storage.getItem(key);
    if (!raw || JSON.stringify(JSON.parse(raw)) !== JSON.stringify(record))
      throw new Error('Pending storage request changed.');
    this.storage.removeItem(key);
    if (this.storage.getItem(key) !== null)
      throw new Error('Could not clear the rejected request.');
  }
}
