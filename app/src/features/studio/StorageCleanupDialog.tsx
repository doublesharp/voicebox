import { HardDrive } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import {
  type CleanupAction,
  type CleanupBody,
  CleanupClient,
  type CleanupPreview,
  CleanupSession,
  type CleanupTransport,
  type RestoreBody,
} from './cleanup-client';
import { errorMessage, type RecoveryStorage } from './session';
import type { StudioProjectSummary } from './types';
import { Field, StudioButton } from './ui';

const storage: RecoveryStorage = {
  get length() {
    return localStorage.length;
  },
  key: (index) => localStorage.key(index),
  getItem: (key) => localStorage.getItem(key),
  setItem: (key, value) => localStorage.setItem(key, value),
  removeItem: (key) => localStorage.removeItem(key),
};
const size = (bytes: number) => `${(bytes / (1024 * 1024)).toFixed(2)} MiB`;

export function StorageCleanupDialog({
  baseUrl,
  projects,
}: {
  baseUrl: string;
  projects: StudioProjectSummary[];
}) {
  const [projectId, setProjectId] = useState('');
  const client = useMemo(() => new CleanupClient(baseUrl), [baseUrl]);
  const project = projects.find((item) => item.id === projectId);
  return (
    <Dialog>
      <DialogTrigger asChild>
        <button type="button" className="studio-quiet" disabled={!projects.length}>
          <HardDrive size={15} /> Review storage
        </button>
      </DialogTrigger>
      <DialogContent className="studio studio-modal">
        <DialogHeader>
          <DialogTitle>Review unused audio</DialogTitle>
          <DialogDescription>
            Protect your performances while keeping unused imports recoverable.
          </DialogDescription>
        </DialogHeader>
        <Field label="Project">
          <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
            <option value="">Choose a project</option>
            {projects.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </Field>
        {project && (
          <StorageCleanupPanel
            key={`${baseUrl}:${project.id}`}
            client={client}
            projectId={project.id}
            recoveryStorage={storage}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

export function StorageCleanupPanel({
  client,
  projectId,
  recoveryStorage,
}: {
  client: CleanupTransport;
  projectId: string;
  recoveryStorage: RecoveryStorage;
}) {
  const session = useMemo(
    () => new CleanupSession(recoveryStorage, client, projectId),
    [recoveryStorage, client, projectId],
  );
  const [preview, setPreview] = useState<CleanupPreview | null>(null);
  const [pending, setPending] = useState<ReturnType<CleanupSession['pending']>>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [restoreIds, setRestoreIds] = useState<string[]>([]);
  const [confirmed, setConfirmed] = useState(false);
  const [message, setMessage] = useState('');
  const generation = useRef(0);
  const reading = useRef(0);
  const active = useRef<object | null>(null);
  const current = useRef(session);
  const refresh = useCallback(async () => {
    const token = generation.current,
      read = ++reading.current;
    try {
      const request = session.pending();
      if (current.current === session) setPending(request);
      const next = await client.preview(projectId);
      if (generation.current !== token || current.current !== session || reading.current !== read)
        return;
      setPreview(next);
      setSelected([]);
      setRestoreIds([]);
      setConfirmed(false);
    } catch (cause) {
      if (generation.current === token && current.current === session && reading.current === read)
        setError(errorMessage(cause));
    }
  }, [session, client, projectId]);
  useEffect(() => {
    current.current = session;
    generation.current++;
    reading.current++;
    setPreview(null);
    setPending(null);
    setSelected([]);
    setRestoreIds([]);
    setConfirmed(false);
    setError('');
    setMessage('');
    setBusy(false);
    active.current = null;
    void refresh();
    return () => {
      generation.current++;
      reading.current++;
    };
  }, [session, refresh]);
  const run = async (action: CleanupAction, replay = false) => {
    if (active.current || (!replay && pending)) return;
    const operation = {};
    active.current = operation;
    const token = generation.current;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const response = await session.submit(
        action,
        () =>
          action === 'cleanup'
            ? ({
                operation_id: crypto.randomUUID(),
                preview_token: preview!.preview_token,
                asset_ids: selected,
              } as CleanupBody)
            : ({ operation_id: crypto.randomUUID(), receipt_ids: restoreIds } as RestoreBody),
        replay ? (pending?.body.operation_id ?? null) : null,
      );
      if (generation.current !== token || current.current !== session) return;
      setPending(session.pending());
      setMessage(
        action === 'cleanup'
          ? `${response.receipts.length} audio files moved to recoverable trash.`
          : `${response.receipts.length} audio files restored.`,
      );
      await refresh();
    } catch (cause) {
      if (generation.current !== token || current.current !== session) return;
      setError(errorMessage(cause));
      try {
        setPending(session.pending());
      } catch (readError) {
        setError(`${errorMessage(cause)} ${errorMessage(readError)}`);
      }
    } finally {
      if (active.current === operation) active.current = null;
      if (generation.current === token && current.current === session) setBusy(false);
    }
  };
  const toggle = (items: string[], ident: string, checked: boolean) =>
    checked
      ? [...items.filter((item) => item !== ident), ident]
      : items.filter((item) => item !== ident);
  return (
    <div>
      <p className="studio-note">
        Drafts, checkpoints, undo/redo, retained takes and recorded jobs stay protected. Only audio
        with no recorded references is offered.
      </p>
      <p className="studio-note">
        Trash remains restorable without automatic deletion. Moving audio there does not free disk
        space; permanent purge is not offered.
      </p>
      {error && (
        <p role="alert" className="studio-error">
          {error}
        </p>
      )}
      {message && <output aria-live="polite">{message}</output>}
      {pending && (
        <div className="studio-banner">
          <p>
            {pending.rejected
              ? 'This request was rejected before acceptance.'
              : 'A storage request has not been acknowledged. Reconcile its original identity before starting another.'}
          </p>
          <StudioButton disabled={busy} onClick={() => void run(pending.action, true)}>
            Retry original {pending.action}
          </StudioButton>
          {pending.rejected && (
            <StudioButton
              disabled={busy}
              onClick={() => {
                try {
                  session.discardRejected();
                  setPending(session.pending());
                  setError('');
                  void refresh();
                } catch (cause) {
                  setError(errorMessage(cause));
                }
              }}
            >
              Discard rejected request
            </StudioButton>
          )}
          <details>
            <summary>Recovery request details</summary>
            <pre>{JSON.stringify(pending.body, null, 2)}</pre>
          </details>
        </div>
      )}
      <StudioButton
        disabled={busy}
        onClick={() => {
          setError('');
          void refresh();
        }}
      >
        Refresh storage preview
      </StudioButton>
      {preview ? (
        <>
          <p>{preview.protected_count} referenced files protected.</p>
          <h3>Unused audio</h3>
          {preview.candidates.length ? (
            preview.candidates.map((item) => (
              <label className="studio-check" key={item.id}>
                <input
                  type="checkbox"
                  disabled={busy || !!pending}
                  checked={selected.includes(item.id)}
                  onChange={(event) => {
                    setSelected(toggle(selected, item.id, event.target.checked));
                    setConfirmed(false);
                  }}
                />
                <span>
                  Audio {item.id.slice(0, 8)} · {size(item.size_bytes)} · {item.duration.toFixed(1)}
                  s
                </span>
              </label>
            ))
          ) : (
            <p>No unreferenced audio in this project.</p>
          )}
          <label className="studio-check">
            <input
              type="checkbox"
              checked={confirmed}
              disabled={busy || !!pending || !selected.length}
              onChange={(event) => setConfirmed(event.target.checked)}
            />{' '}
            I reviewed the selected files and want to move them to recoverable trash.
          </label>
          <StudioButton
            disabled={busy || !!pending || !confirmed || !selected.length}
            onClick={() => void run('cleanup')}
          >
            Move selected audio to trash
          </StudioButton>
          <h3>Recoverable trash</h3>
          {preview.trash.length ? (
            preview.trash.map((item) => (
              <label className="studio-check" key={item.id}>
                <input
                  type="checkbox"
                  disabled={busy || !!pending}
                  checked={restoreIds.includes(item.id)}
                  onChange={(event) =>
                    setRestoreIds(toggle(restoreIds, item.id, event.target.checked))
                  }
                />
                <span>
                  Audio {item.asset_id.slice(0, 8)} · {size(item.size_bytes)}
                </span>
              </label>
            ))
          ) : (
            <p>No audio in recoverable trash.</p>
          )}
          <StudioButton
            disabled={busy || !!pending || !restoreIds.length}
            onClick={() => void run('restore')}
          >
            Restore selected audio
          </StudioButton>
        </>
      ) : (
        <p>Opening storage preview…</p>
      )}
    </div>
  );
}
