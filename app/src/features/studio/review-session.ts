import {
  type CheckpointReview,
  isReviewContent,
  type ReviewItem,
  type ReviewRequest,
  type ReviewTransport,
} from './review-client';
import {
  errorMessage,
  operationId,
  type RecoveryStorage,
  type SaveStatus,
  storageKeys,
} from './session';
export const defaultReviewChecklist = (): ReviewItem[] => [
  { id: 'pronunciation', label: 'Pronunciation and wording checked', checked: false },
  { id: 'pacing', label: 'Pacing and timing checked', checked: false },
  { id: 'audio', label: 'Rendered audio listened to', checked: false },
];
type Draft = Pick<CheckpointReview, 'note' | 'checklist'>;
type Pending =
  | { kind: 'save'; body: ReviewRequest }
  | { kind: 'approval'; body: Pick<ReviewRequest, 'operation_id' | 'expected_version'> };
interface Journal {
  version: 1;
  project_id: string;
  revision_id: string;
  base_version: number;
  draft: Draft | null;
  pending: Pending | null;
}
interface Recovery {
  key: string;
  raw: string;
  journal: Journal | null;
}
export interface ReviewState {
  review: CheckpointReview | null;
  draft: Draft | null;
  status: SaveStatus;
  error: string | null;
  recoverySaved: boolean;
  recoveries: { key: string; readable: boolean }[];
}
const activeKeys = new Set<string>();
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
function parse(raw: string, project: string, revision: string): Journal | null {
  try {
    const j = JSON.parse(raw) as Journal;
    const draftValid = (d: Draft | null) => d === null || isReviewContent(d);
    if (
      j.version !== 1 ||
      j.project_id !== project ||
      j.revision_id !== revision ||
      !Number.isInteger(j.base_version) ||
      j.base_version < 0 ||
      !draftValid(j.draft)
    )
      return null;
    if (
      j.pending !== null &&
      (!j.pending ||
        !['save', 'approval'].includes(j.pending.kind) ||
        typeof j.pending.body?.operation_id !== 'string' ||
        !Number.isInteger(j.pending.body.expected_version) ||
        j.pending.body.expected_version < 0 ||
        (j.pending.kind === 'save' && !draftValid(j.pending.body)))
    )
      return null;
    return j;
  } catch {
    return null;
  }
}
/** Owns every keystroke, its original review version and uncertain request identity. */
export class ReviewSession {
  private state: ReviewState = {
    review: null,
    draft: null,
    status: 'loading',
    error: null,
    recoverySaved: true,
    recoveries: [],
  };
  private listeners = new Set<() => void>();
  private baseVersion: number | null = null;
  private pending: Pending | null = null;
  private offered: Recovery[] = [];
  private active: Promise<boolean> | null = null;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private maxTimer: ReturnType<typeof setTimeout> | undefined;
  private disposed = false;
  private epoch = 0;
  private loadEpoch = 0;
  readonly prefix: string;
  readonly key: string;
  constructor(
    readonly client: ReviewTransport,
    readonly project: string,
    readonly revision: string,
    readonly storage: RecoveryStorage,
  ) {
    this.prefix = `voicebox-studio:review:${encodeURIComponent(client.baseUrl)}:${encodeURIComponent(project)}:${encodeURIComponent(revision)}:session:`;
    this.key = this.prefix + operationId();
  }
  getSnapshot = () => this.state;
  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };
  private emit(patch: Partial<ReviewState>) {
    if (this.disposed) return;
    this.state = { ...this.state, ...patch };
    for (const fn of this.listeners) fn();
  }
  private consumed(key: string) {
    return `voicebox-studio:review-consumed:${encodeURIComponent(key)}`;
  }
  private scan() {
    this.offered = [];
    try {
      for (const key of storageKeys(this.storage)) {
        if (!key.startsWith(this.prefix) || key === this.key || activeKeys.has(key)) continue;
        const raw = this.storage.getItem(key);
        if (!raw || this.storage.getItem(this.consumed(key)) === raw) continue;
        this.offered.push({ key, raw, journal: parse(raw, this.project, this.revision) });
      }
      this.emit({ recoveries: this.offered.map((r) => ({ key: r.key, readable: !!r.journal })) });
    } catch (error) {
      this.emit({ recoverySaved: false, error: errorMessage(error) });
    }
  }
  async load() {
    this.disposed = false;
    const token = ++this.loadEpoch;
    activeKeys.add(this.key);
    try {
      const result = await this.client.get(this.project, this.revision);
      if (this.disposed || token !== this.loadEpoch) return;
      this.scan();
      this.emit({ review: result.review, status: this.offered.length ? 'recovery' : 'saved' });
    } catch (error) {
      if (token === this.loadEpoch) this.emit({ status: 'error', error: errorMessage(error) });
    }
  }
  value(): Draft {
    return (
      this.state.draft ?? {
        note: this.state.review?.note ?? '',
        checklist:
          this.state.review && this.state.review.version > 0
            ? this.state.review.checklist
            : defaultReviewChecklist(),
      }
    );
  }
  edit(edit: Partial<Draft>) {
    if (!this.state.review || ['loading', 'recovery'].includes(this.state.status)) return;
    this.baseVersion ??= this.state.review.version;
    this.emit({
      draft: structuredClone({ ...this.value(), ...edit }),
      status: ['error', 'conflict'].includes(this.state.status)
        ? this.state.status
        : this.active
          ? 'saving'
          : 'dirty',
    });
    this.persist();
    if (['error', 'conflict'].includes(this.state.status)) return;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => void this.flush(), 450);
    this.maxTimer ??= setTimeout(() => void this.flush(), 2000);
  }
  private journal(): Journal {
    return {
      version: 1,
      project_id: this.project,
      revision_id: this.revision,
      base_version: this.baseVersion ?? this.state.review?.version ?? 0,
      draft: this.state.draft,
      pending: this.pending,
    };
  }
  recoveryCopy() {
    return JSON.stringify(
      this.offered.length
        ? {
            current: this.journal(),
            journals: this.offered.map(({ key, raw }) => ({ key, raw })),
          }
        : this.journal(),
      null,
      2,
    );
  }
  private persist() {
    try {
      if (this.pending || this.state.draft) {
        const raw = JSON.stringify(this.journal());
        this.storage.setItem(this.key, raw);
        if (this.storage.getItem(this.key) !== raw)
          throw new Error('Review recovery write was not retained');
      } else {
        this.storage.removeItem(this.key);
        this.baseVersion = null;
      }
      this.emit({ recoverySaved: true });
      return true;
    } catch (error) {
      this.emit({
        recoverySaved: false,
        error: `Local review recovery unavailable: ${errorMessage(error)}`,
      });
      return false;
    }
  }
  private clearTimers() {
    clearTimeout(this.timer);
    clearTimeout(this.maxTimer);
    this.timer = undefined;
    this.maxTimer = undefined;
  }
  async flush(): Promise<boolean> {
    this.clearTimers();
    if (this.active) {
      await this.active;
      return this.flush();
    }
    if (
      this.disposed ||
      !this.state.review ||
      ['loading', 'recovery', 'conflict', 'error'].includes(this.state.status)
    )
      return false;
    this.active = this.drain().finally(() => {
      this.active = null;
    });
    return this.active;
  }
  private async drain(): Promise<boolean> {
    while (!this.disposed && (this.pending || this.state.draft)) {
      if (!this.pending) {
        const draft = structuredClone(this.state.draft!);
        this.pending = {
          kind: 'save',
          body: {
            operation_id: operationId(),
            expected_version: this.baseVersion ?? this.state.review!.version,
            ...draft,
          },
        };
      }
      if (!this.persist()) {
        this.emit({ status: 'error' });
        return false;
      }
      const entry = this.pending;
      this.epoch++;
      this.emit({ status: 'saving', error: null });
      try {
        const response =
          entry.kind === 'save'
            ? await this.client.save(this.project, this.revision, entry.body)
            : await this.client.approve(this.project, this.revision, entry.body);
        if (this.disposed) return false; // Journal retains uncertain operation for exact replay.
        this.pending = null;
        let draft = this.state.draft;
        if (
          entry.kind === 'save' &&
          same(draft, { note: entry.body.note, checklist: entry.body.checklist })
        )
          draft = null;
        const newer = (this.state.review?.version ?? 0) > response.review.version;
        this.baseVersion = response.review.version;
        this.emit({ review: newer ? this.state.review : response.review, draft });
        this.persist();
        if (newer && draft) {
          this.emit({
            status: 'conflict',
            error:
              'This operation committed before a newer review. Your remaining edits are preserved.',
          });
          return false;
        }
      } catch (error) {
        const status =
          error && typeof error === 'object' && 'status' in error ? error.status : undefined;
        if (status === 422) this.pending = null;
        this.emit({ status: status === 409 ? 'conflict' : 'error', error: errorMessage(error) });
        this.persist();
        return false;
      }
    }
    this.emit({ status: 'saved', error: null });
    return !this.disposed;
  }
  async retry() {
    if (['conflict', 'recovery'].includes(this.state.status)) return false;
    if (!this.state.review) {
      await this.load();
      return false;
    }
    this.emit({ status: 'dirty' });
    return this.flush();
  }
  async approve() {
    if (
      !this.state.review ||
      ['loading', 'conflict', 'recovery', 'error', 'saving'].includes(this.state.status)
    )
      return false;
    // Even an empty review is acknowledged before it can be approved. Checklist completion is optional.
    if (!this.state.review.version && !this.state.draft) this.edit(this.value());
    if (!(await this.flush()) || this.state.draft || this.pending) return false;
    this.pending = {
      kind: 'approval',
      body: { operation_id: operationId(), expected_version: this.state.review!.version },
    };
    return this.flush();
  }
  recover(key: string) {
    const record = this.offered.find((r) => r.key === key);
    if (!record?.journal || !this.state.review || this.pending || this.state.draft || this.active)
      return;
    try {
      this.storage.setItem(this.key, record.raw);
      if (this.storage.getItem(this.key) !== record.raw)
        throw new Error('Recovery copy did not persist');
      this.storage.setItem(this.consumed(key), record.raw);
    } catch (error) {
      this.emit({ recoverySaved: false, error: errorMessage(error) });
      return;
    }
    const j = record.journal;
    this.pending = j.pending;
    this.baseVersion = j.base_version;
    this.offered = this.offered.filter((r) => r.key !== key);
    const conflict = !j.pending && this.state.review.version !== j.base_version;
    this.emit({
      draft: j.draft,
      status: conflict ? 'conflict' : 'dirty',
      error: conflict
        ? 'The saved review changed while this copy was offline. Export your copy before reloading.'
        : null,
      recoveries: this.offered.map((r) => ({ key: r.key, readable: !!r.journal })),
    });
    this.persist();
  }
  discardRecovery(key: string) {
    const record = this.offered.find((r) => r.key === key);
    if (!record) return;
    try {
      this.storage.setItem(this.consumed(key), record.raw);
      this.offered = this.offered.filter((r) => r.key !== key);
      this.emit({
        recoveries: this.offered.map((r) => ({ key: r.key, readable: !!r.journal })),
        status:
          this.state.draft || this.pending
            ? this.state.status
            : this.offered.length
              ? 'recovery'
              : 'saved',
      });
    } catch (error) {
      this.emit({ recoverySaved: false, error: errorMessage(error) });
    }
  }
  async reloadSaved() {
    if (this.active) await this.active;
    this.clearTimers();
    this.emit({ status: 'loading' });
    try {
      const result = await this.client.get(this.project, this.revision);
      // Discard only this session's active draft; each offered copy has its own explicit action.
      this.storage.removeItem(this.key);
      this.pending = null;
      this.baseVersion = null;
      this.epoch++;
      this.emit({
        review: result.review,
        draft: null,
        status: this.offered.length ? 'recovery' : 'saved',
        error: null,
        recoveries: this.offered.map((r) => ({ key: r.key, readable: !!r.journal })),
        recoverySaved: true,
      });
    } catch (error) {
      this.emit({ status: 'error', error: errorMessage(error), recoverySaved: false });
    }
  }
  async refresh() {
    if (!this.state.review || this.active || this.disposed) return;
    const epoch = this.epoch;
    try {
      const { review } = await this.client.get(this.project, this.revision);
      if (this.disposed || this.active || epoch !== this.epoch) return;
      if (review.version <= this.state.review!.version) return;
      if (this.pending) {
        this.emit({ review });
        return;
      }
      if (this.state.draft || this.offered.length) {
        this.clearTimers();
        this.emit({
          review,
          status: this.offered.length ? 'recovery' : 'conflict',
          error:
            'Another window saved this review. Your original review version and local edits are preserved.',
        });
      } else this.emit({ review });
    } catch {
      /* Read outages do not change acknowledged save status. */
    }
  }
  dispose() {
    this.clearTimers();
    this.disposed = true;
    this.loadEpoch++;
    activeKeys.delete(this.key);
    this.listeners.clear();
  }
}
/** Lives at project scope so checkpoint and workspace navigation retain each resource's owner. */
export class ReviewSessions {
  private sessions = new Map<string, ReviewSession>();
  private listeners = new Set<() => void>();
  private version = 0;
  constructor(
    readonly client: ReviewTransport,
    readonly project: string,
    readonly storage: RecoveryStorage,
  ) {}
  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };
  getSnapshot = () => this.version;
  get(revision: string) {
    let session = this.sessions.get(revision);
    if (!session) {
      session = new ReviewSession(this.client, this.project, revision, this.storage);
      this.sessions.set(revision, session);
      session.subscribe(() => {
        this.version++;
        for (const fn of this.listeners) fn();
      });
      void session.load();
    }
    return session;
  }
  unsafeToLeave() {
    return [...this.sessions.values()].some((s) => {
      const v = s.getSnapshot();
      return !v.recoverySaved && v.status !== 'saved';
    });
  }
  async refresh() {
    await Promise.all([...this.sessions.values()].map((s) => s.refresh()));
  }
  dispose() {
    for (const session of this.sessions.values()) session.dispose();
    this.sessions.clear();
    this.listeners.clear();
  }
}
