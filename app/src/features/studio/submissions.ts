import { useEffect, useMemo, useReducer } from 'react';
import type { StudioClient } from './client';
import { errorMessage, type RecoveryStorage, storageKeys } from './session';

type SubmissionBodies = {
  generation: Parameters<StudioClient['generate']>[1];
  render: Parameters<StudioClient['render']>[1];
};
type Kind = keyof SubmissionBodies;
interface RecordBody<K extends Kind> {
  version: 1;
  kind: K;
  body: SubmissionBodies[K];
  rejected?: boolean;
}

/** Each intent owns one record. An uncertain mutation is replayed only on an explicit action. */
export class DurableSubmission<K extends Kind> {
  private readonly prefix: string;
  private busy = false;
  constructor(
    private readonly storage: RecoveryStorage,
    endpoint: string,
    projectId: string,
    private readonly kind: K,
  ) {
    this.prefix = `voicebox-studio:submission:${encodeURIComponent(endpoint)}:${projectId}:${kind}:`;
  }
  pending() {
    return storageKeys(this.storage)
      .filter((key) => key.startsWith(this.prefix))
      .sort()
      .map((key) => {
        const raw = this.storage.getItem(key);
        if (!raw)
          throw new Error('A pending submission changed. Reopen the workspace to reconcile it.');
        const record = JSON.parse(raw) as RecordBody<K>;
        const body = record.body;
        if (
          record.version !== 1 ||
          record.kind !== this.kind ||
          !body ||
          typeof body.idempotency_key !== 'string' ||
          key !== this.prefix + body.idempotency_key ||
          (this.kind === 'generation' &&
            (!('passage_ids' in body) || !Array.isArray(body.passage_ids))) ||
          (this.kind === 'render' &&
            (!('revision_id' in body) || typeof body.revision_id !== 'string'))
        )
          throw new Error(
            'Unreadable pending studio submission. Preserve local storage before recovery.',
          );
        return { key, raw, body, rejected: record.rejected === true };
      });
  }
  discardRejected() {
    const record = this.pending()[0];
    if (!record?.rejected)
      throw new Error('An uncertain submission must be reconciled with its original identity.');
    if (this.storage.getItem(record.key) !== record.raw)
      throw new Error('The pending submission changed. Reconcile it first.');
    this.storage.removeItem(record.key);
    if (this.storage.getItem(record.key) !== null)
      throw new Error('Could not clear the rejected submission.');
  }
  async submit(
    create: () => SubmissionBodies[K],
    send: (body: SubmissionBodies[K]) => Promise<unknown>,
    changed: () => void = () => {},
    expectedId?: string | null,
  ) {
    if (this.busy) return;
    this.busy = true;
    try {
      const previous = this.pending()[0];
      if (expectedId !== undefined && (previous?.body.idempotency_key ?? null) !== expectedId)
        throw new Error(
          'The pending submission changed. Review the original request before retrying.',
        );
      const body = previous?.body ?? structuredClone(create());
      const key = previous?.key ?? this.prefix + body.idempotency_key;
      // Every attempt becomes uncertain before it is issued, even if an earlier attempt
      // was definitively rejected. Retain the original body and revoke discard eligibility.
      const raw = JSON.stringify({ version: 1, kind: this.kind, body });
      // Verify the exact serialized identity before a network mutation can be issued.
      this.storage.setItem(key, raw);
      if (this.storage.getItem(key) !== raw)
        throw new Error('Submission storage did not retain the request.');
      changed();
      try {
        await send(structuredClone(body));
      } catch (error) {
        // The contract's 422 is a definitive pre-acceptance validation rejection. A lost
        // response, 409 identity conflict or server failure remains uncertain and cannot be cleared.
        if (
          typeof error === 'object' &&
          error !== null &&
          'status' in error &&
          error.status === 422 &&
          this.storage.getItem(key) === raw
        )
          this.storage.setItem(
            key,
            JSON.stringify({ version: 1, kind: this.kind, body, rejected: true }),
          );
        throw error;
      }
      // A successful response clears only this exact request; other windows keep their records.
      if (this.storage.getItem(key) === raw) this.storage.removeItem(key);
      if (this.storage.getItem(key) === raw)
        throw new Error(
          'Could not clear the acknowledged submission. Reconcile the same request before continuing.',
        );
    } finally {
      this.busy = false;
      changed();
    }
  }
}

export function useDurableSubmission<K extends Kind>(
  storage: RecoveryStorage,
  endpoint: string,
  projectId: string,
  kind: K,
) {
  const journal = useMemo(
    () => new DurableSubmission(storage, endpoint, projectId, kind),
    [storage, endpoint, projectId, kind],
  );
  const [, changed] = useReducer((value: number) => value + 1, 0);
  useEffect(() => {
    if (typeof window === 'undefined') return;
    window.addEventListener('storage', changed);
    return () => window.removeEventListener('storage', changed);
  }, []);
  let request: SubmissionBodies[K] | null = null;
  let rejected = false;
  let error: string | null = null;
  try {
    const pending = journal.pending()[0];
    request = pending?.body ?? null;
    rejected = pending?.rejected ?? false;
  } catch (fault) {
    error = `Cannot safely persist or recover this submission: ${errorMessage(fault)}`;
  }
  const [failure, setFailure] = useReducer(
    (
      _value: { journal: DurableSubmission<K>; message: string } | null,
      value: { journal: DurableSubmission<K>; message: string } | null,
    ) => value,
    null,
  );
  const submit = async (
    create: () => SubmissionBodies[K],
    send: (body: SubmissionBodies[K]) => Promise<unknown>,
  ) => {
    setFailure(null);
    try {
      await journal.submit(create, send, changed, request?.idempotency_key ?? null);
    } catch (fault) {
      setFailure({
        journal,
        message: `Could not persist or reconcile the submission: ${errorMessage(fault)}`,
      });
      throw fault;
    }
  };
  const discardRejected = () => {
    try {
      journal.discardRejected();
      setFailure(null);
    } catch (fault) {
      setFailure({ journal, message: errorMessage(fault) });
    }
    changed();
  };
  return {
    request,
    rejected,
    discardRejected,
    error: error ?? (failure?.journal === journal ? failure.message : null),
    submit,
  };
}
