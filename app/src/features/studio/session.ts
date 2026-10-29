import type { PassageEdit, StudioCommandType, StudioProject, StudioTransport } from './types';

export type SaveStatus =
  | 'loading'
  | 'saved'
  | 'dirty'
  | 'saving'
  | 'error'
  | 'conflict'
  | 'recovery';
export interface SessionState {
  project: StudioProject | null;
  drafts: Record<string, PassageEdit>;
  takeDrafts: Record<string, TakeAnnotationEdit>;
  status: SaveStatus;
  error: string | null;
  recoverySaved: boolean;
}
type TakeAnnotationEdit = { name?: string; note?: string };
export interface RecoveryStorage {
  readonly length?: number;
  key?(index: number): string | null;
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}
/** The app's storage adapter delegates to localStorage; tests may provide enumeration directly. */
export function storageKeys(storage: RecoveryStorage): string[] {
  const source = storage.key ? storage : typeof localStorage !== 'undefined' ? localStorage : null;
  if (!source?.key || source.length === undefined)
    throw new Error('Cannot enumerate durable studio recovery storage.');
  return Array.from({ length: source.length }, (_, index) => source.key!(index)).filter(
    (key): key is string => key !== null,
  );
}
interface PendingMutation {
  operation_id: string;
  expected_revision?: number;
  kind: 'command' | 'checkpoint' | 'approval';
  type?: StudioCommandType;
  payload: Record<string, unknown>;
  edit?: { id: string; value: PassageEdit };
  takeEdit?: { id: string; value: TakeAnnotationEdit };
}
interface RecoveryJournal {
  version: 1 | 2;
  project_id: string;
  base_revision: number;
  drafts: Record<string, PassageEdit>;
  takeDrafts?: Record<string, TakeAnnotationEdit>;
  queue: PendingMutation[];
}
interface RecoveryRecord {
  key: string;
  raw: string;
  journal: RecoveryJournal | null;
}
const activeJournals = new Set<string>();
export const operationId = () => crypto.randomUUID();
export const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : String(error);
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** The only writer for a mounted project. Never retries a mutation implicitly. */
export class ProjectSession {
  private state: SessionState = {
    project: null,
    drafts: {},
    takeDrafts: {},
    status: 'loading',
    error: null,
    recoverySaved: true,
  };
  private listeners = new Set<() => void>();
  private queue: PendingMutation[] = [];
  private recovered: RecoveryRecord[] = [];
  private unreadableRecovery: string | null = null;
  private journalBaseRevision: number | null = null;
  private active: Promise<void> | null = null;
  private disposed = false;
  private epoch = 0;
  private loadEpoch = 0;
  private refreshing = false;
  private idleTimer: ReturnType<typeof setTimeout> | undefined;
  private maxTimer: ReturnType<typeof setTimeout> | undefined;
  readonly key: string;
  private readonly legacyKey: string;

  constructor(
    readonly client: StudioTransport,
    readonly id: string,
    readonly storage: RecoveryStorage,
  ) {
    this.legacyKey = `voicebox-studio:recovery:${encodeURIComponent(client.baseUrl)}:${id}`;
    this.key = `${this.legacyKey}:session:${operationId()}`;
  }
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private emit(patch: Partial<SessionState>) {
    if (this.disposed) return;
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener();
  }
  private blocked() {
    return ['loading', 'conflict', 'error', 'recovery'].includes(this.state.status);
  }
  private consumedKey(key: string) {
    return `voicebox-studio:consumed:${encodeURIComponent(key)}`;
  }
  private readRecovery(): RecoveryRecord[] {
    try {
      const keys = new Set([this.key, this.legacyKey, ...storageKeys(this.storage)]);
      const records: RecoveryRecord[] = [];
      for (const key of keys) {
        if (key !== this.legacyKey && !key.startsWith(`${this.legacyKey}:session:`)) continue;
        if (key !== this.key && activeJournals.has(key)) continue;
        const raw = this.storage.getItem(key);
        if (!raw || this.storage.getItem(this.consumedKey(key)) === raw) continue;
        let journal: RecoveryJournal | null = null;
        try {
          const value = JSON.parse(raw) as RecoveryJournal;
          if (
            (value.version === 1 || value.version === 2) &&
            value.project_id === this.id &&
            Number.isInteger(value.base_revision) &&
            value.drafts &&
            (value.version === 1 ||
              (value.takeDrafts &&
                typeof value.takeDrafts === 'object' &&
                !Array.isArray(value.takeDrafts))) &&
            Array.isArray(value.queue)
          )
            journal = value;
        } catch {
          /* Preserve malformed records for export and explicit discard. */
        }
        records.push({ key, raw, journal });
      }
      this.unreadableRecovery = records.find((record) => !record.journal)?.raw ?? null;
      return records;
    } catch (error) {
      this.emit({ recoverySaved: false, error: errorMessage(error) });
      return [];
    }
  }
  async load() {
    this.disposed = false;
    activeJournals.add(this.key);
    const token = ++this.loadEpoch;
    try {
      const project = await this.client.project(this.id);
      if (this.disposed || token !== this.loadEpoch) return;
      this.recovered = this.readRecovery();
      this.emit({
        project,
        status: this.recovered.length ? 'recovery' : 'saved',
      });
    } catch (error) {
      if (token === this.loadEpoch) this.emit({ status: 'error', error: errorMessage(error) });
    }
  }
  hasRecoverableEdits() {
    return this.recovered.some((record) => record.journal);
  }
  recoverableJournals() {
    return this.recovered.map(({ key, journal }) => ({
      key,
      baseRevision: journal?.base_revision,
      readable: !!journal,
    }));
  }
  recoveryCopy() {
    if (this.recovered.length > 1)
      return JSON.stringify(
        { journals: this.recovered.map(({ key, raw }) => ({ key, raw })) },
        null,
        2,
      );
    if (this.unreadableRecovery) return this.unreadableRecovery;
    if (this.recovered.length) return this.recovered[0].raw;
    return this.currentJournal();
  }
  private currentJournal() {
    return JSON.stringify(
      {
        version: 2,
        project_id: this.id,
        base_revision: this.journalBaseRevision ?? this.state.project?.revision ?? 0,
        drafts: this.state.drafts,
        takeDrafts: this.state.takeDrafts,
        queue: this.queue,
      },
      null,
      2,
    );
  }
  private persist() {
    if (this.unreadableRecovery) return;
    try {
      if (!this.queue.length && !this.hasDrafts()) {
        this.storage.removeItem(this.key);
        this.journalBaseRevision = null;
      } else this.storage.setItem(this.key, this.currentJournal());
      this.emit({ recoverySaved: true });
    } catch {
      this.emit({ recoverySaved: false });
    }
  }
  private clearTimers() {
    clearTimeout(this.idleTimer);
    clearTimeout(this.maxTimer);
    this.idleTimer = undefined;
    this.maxTimer = undefined;
  }
  private hasDrafts() {
    return (
      Object.keys(this.state.drafts).length > 0 || Object.keys(this.state.takeDrafts).length > 0
    );
  }
  editTakeAnnotation(id: string, edit: TakeAnnotationEdit) {
    if (
      !this.state.project?.takes.some((take) => take.id === id) ||
      this.state.status === 'recovery'
    )
      return;
    this.journalBaseRevision ??= this.state.project.revision;
    this.emit({
      takeDrafts: { ...this.state.takeDrafts, [id]: { ...this.state.takeDrafts[id], ...edit } },
      status: this.blocked() ? this.state.status : this.active ? 'saving' : 'dirty',
    });
    this.persist();
    if (this.blocked()) return;
    clearTimeout(this.idleTimer);
    this.idleTimer = setTimeout(() => void this.flush(), 450);
    this.maxTimer ??= setTimeout(() => void this.flush(), 2000);
  }
  editPassage(id: string, edit: PassageEdit) {
    const passage = this.state.project?.document.passages.find((p) => p.id === id);
    if (!passage || passage.locked || this.state.status === 'recovery') return;
    this.journalBaseRevision ??= this.state.project!.revision;
    const value = { ...this.state.drafts[id], ...edit };
    const drafts = { ...this.state.drafts, [id]: value };
    this.emit({
      drafts,
      status: this.blocked() ? this.state.status : this.active ? 'saving' : 'dirty',
    });
    this.persist();
    if (this.blocked()) return;
    clearTimeout(this.idleTimer);
    this.idleTimer = setTimeout(() => void this.flush(), 450);
    this.maxTimer ??= setTimeout(() => void this.flush(), 2000);
  }
  private captureDrafts() {
    for (const [id, value] of Object.entries(this.state.drafts)) {
      if (this.queue.some((entry) => entry.edit?.id === id && same(entry.edit.value, value)))
        continue;
      this.queue.push({
        operation_id: operationId(),
        kind: 'command',
        type: 'update_passage',
        payload: { passage_id: id, ...structuredClone(value) },
        edit: { id, value: structuredClone(value) },
      });
    }
    for (const [id, value] of Object.entries(this.state.takeDrafts)) {
      if (
        this.queue.some((entry) => entry.takeEdit?.id === id && same(entry.takeEdit.value, value))
      )
        continue;
      this.queue.push({
        operation_id: operationId(),
        kind: 'command',
        type: 'annotate_take',
        payload: { take_id: id, ...structuredClone(value) },
        takeEdit: { id, value: structuredClone(value) },
      });
    }
  }
  async flush() {
    this.clearTimers();
    if (this.disposed || this.blocked() || !this.state.project) return;
    this.captureDrafts();
    return this.drain();
  }
  private drain(): Promise<void> {
    if (this.active) return this.active;
    if (!this.queue.length) return Promise.resolve();
    this.emit({ status: 'saving', error: null });
    this.active = this.runQueue().finally(() => {
      this.active = null;
    });
    return this.active;
  }
  private async runQueue() {
    while (this.queue.length && !this.disposed) {
      const entry = this.queue[0];
      const project = this.state.project;
      if (!project) return;
      entry.expected_revision ??= project.revision;
      this.epoch++;
      this.persist();
      try {
        let result: StudioProject;
        if (entry.kind === 'checkpoint') {
          result = await this.client.checkpoint(this.id, {
            operation_id: entry.operation_id,
            expected_revision: entry.expected_revision,
            name: String(entry.payload.name),
          });
        } else if (entry.kind === 'approval') {
          result = await this.client.approve(this.id, {
            operation_id: entry.operation_id,
            revision_id: String(entry.payload.revision_id),
            note: String(entry.payload.note ?? ''),
          });
        } else {
          result = await this.client.command(this.id, {
            operation_id: entry.operation_id,
            expected_revision: entry.expected_revision,
            type: entry.type!,
            payload: entry.payload,
          });
        }
        if (this.disposed) return; // Leave the journal for idempotent recovery of a lost response.
        this.queue.shift();
        const newerHead = result.revision < (this.state.project?.revision ?? 0);
        this.journalBaseRevision = result.revision;
        const drafts = { ...this.state.drafts };
        const takeDrafts = { ...this.state.takeDrafts };
        if (entry.edit && same(drafts[entry.edit.id], entry.edit.value))
          delete drafts[entry.edit.id];
        if (entry.takeEdit && same(takeDrafts[entry.takeEdit.id], entry.takeEdit.value))
          delete takeDrafts[entry.takeEdit.id];
        this.emit({
          project:
            result.revision >= (this.state.project?.revision ?? 0) ? result : this.state.project,
          drafts,
          takeDrafts,
        });
        this.persist();
        if (newerHead && (this.queue.length || this.hasDrafts())) {
          this.emit({
            status: 'conflict',
            error:
              'The recovered operation committed before a newer saved draft. Remaining local edits are preserved; export a recovery copy before reloading.',
          });
          return;
        }
      } catch (error) {
        const status =
          typeof error === 'object' && error !== null && 'status' in error
            ? error.status
            : undefined;
        const conflict = status === 409;
        // A validation rejection did not commit. Keep editable drafts, discard only this invalid request.
        if (status === 422) this.queue.shift();
        this.emit({ status: conflict ? 'conflict' : 'error', error: errorMessage(error) });
        this.persist();
        return;
      }
    }
    if (!this.hasDrafts() && !this.disposed) this.recovered = this.readRecovery();
    this.emit({
      status: this.hasDrafts() ? 'dirty' : this.recovered.length ? 'recovery' : 'saved',
    });
  }
  async command(type: StudioCommandType, payload: Record<string, unknown> = {}): Promise<boolean> {
    return this.mutate({ operation_id: operationId(), kind: 'command', type, payload });
  }
  async checkpoint(name: string) {
    return this.mutate({ operation_id: operationId(), kind: 'checkpoint', payload: { name } });
  }
  async approve(revision_id: string, note: string) {
    return this.mutate({
      operation_id: operationId(),
      kind: 'approval',
      payload: { revision_id, note },
    });
  }
  private async mutate(entry: PendingMutation): Promise<boolean> {
    await this.flush();
    if (this.disposed || this.blocked() || !this.state.project) return false;
    this.queue.push(entry);
    this.persist();
    await this.drain();
    return !this.blocked();
  }
  async retry() {
    if (this.state.status === 'conflict' || this.state.status === 'recovery') return;
    if (!this.state.project) return this.load();
    this.emit({ status: 'dirty', error: null });
    // Replay the exact uncertain request first. Do not give it a new identity/revision.
    if (this.queue.length) await this.drain();
    if (!this.blocked()) await this.flush();
  }
  recover(key?: string) {
    const record = this.recovered.find((record) => record.journal && (!key || record.key === key));
    if (!record?.journal || !this.state.project) return;
    const recovery = record.journal;
    // Copy into this editing session before consuming the offered snapshot. The source stays
    // intact, and a concurrent writer's changed source will be offered again on restart.
    try {
      this.storage.setItem(this.key, record.raw);
      if (this.storage.getItem(this.key) !== record.raw)
        throw new Error('Recovery copy could not persist.');
      if (record.key !== this.key) this.storage.setItem(this.consumedKey(record.key), record.raw);
    } catch (error) {
      this.emit({ recoverySaved: false, error: errorMessage(error) });
      return;
    }
    this.recovered = [];
    this.unreadableRecovery = null;
    this.queue = recovery.queue;
    this.journalBaseRevision = recovery.base_revision;
    const changed =
      recovery.base_revision !== this.state.project.revision &&
      this.queue[0]?.expected_revision === undefined;
    this.emit({
      drafts: recovery.drafts,
      takeDrafts: recovery.takeDrafts ?? {},
      status: changed ? 'conflict' : 'dirty',
      error: changed
        ? 'The saved draft changed while this recovery copy was offline. Export your copy before reloading.'
        : null,
    });
    this.persist();
  }
  async reloadSaved() {
    if (this.active) await this.active;
    const token = ++this.loadEpoch;
    try {
      const project = await this.client.project(this.id);
      if (this.disposed || token !== this.loadEpoch) return;
      this.clearTimers();
      const offered = this.recovered[0];
      if (offered && offered.key !== this.key)
        this.storage.setItem(this.consumedKey(offered.key), offered.raw);
      this.recovered = [];
      this.unreadableRecovery = null;
      this.journalBaseRevision = null;
      this.queue = [];
      this.epoch++;
      this.emit({ project, drafts: {}, takeDrafts: {}, status: 'saved', error: null });
      this.persist();
      this.recovered = this.readRecovery();
      if (this.recovered.length) this.emit({ status: 'recovery' });
    } catch (error) {
      this.emit({ error: errorMessage(error) });
    }
  }
  async refresh() {
    if (this.disposed || this.active || this.refreshing || this.state.status === 'loading') return;
    const epoch = this.epoch;
    this.refreshing = true;
    try {
      const project = this.client.refreshProject
        ? await this.client.refreshProject(this.id, this.state.project?.change_token)
        : await this.client.project(this.id);
      if (!project || this.disposed || epoch !== this.epoch || this.active) return;
      const currentRevision = this.state.project?.revision ?? 0;
      if (
        project.revision > currentRevision &&
        (this.hasDrafts() || this.queue.length || this.recovered.length)
      ) {
        // Polls may add job/asset information, but may never rebase local editing intent.
        const current = this.state.project;
        if (current)
          this.emit({
            project: {
              ...current,
              jobs: project.jobs,
              takes: project.takes,
              assets: project.assets,
              deliverables: project.deliverables,
              revisions: project.revisions,
              approvals: project.approvals,
              change_token: project.change_token,
            },
          });
        if (!this.queue.length && !this.recovered.length) {
          this.clearTimers();
          this.emit({
            status: 'conflict',
            error: `The server draft advanced to revision ${project.revision}. Your local edits are preserved. Export a recovery copy before reloading.`,
          });
        }
      } else if (project.revision >= currentRevision) this.emit({ project });
    } catch {
      // A read outage is not a failed durable save. The next poll can reconnect.
    } finally {
      this.refreshing = false;
    }
  }
  dispose() {
    this.clearTimers();
    this.disposed = true;
    activeJournals.delete(this.key);
    this.listeners.clear();
    this.loadEpoch++;
  }
}
