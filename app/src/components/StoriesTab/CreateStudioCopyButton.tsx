import { useNavigate } from '@tanstack/react-router';
import { Copy } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { errorMessage } from '@/features/studio/session';
import { StoryCopy, storyCopyStorage } from '@/features/studio/story-copy';
import { useServerStore } from '@/stores/serverStore';

export function CreateStudioCopyButton({ storyId }: { storyId: string }) {
  const endpoint = useServerStore((state) => state.serverUrl);
  const navigate = useNavigate();
  const copy = useMemo(
    () => new StoryCopy(storyCopyStorage, endpoint, storyId),
    [endpoint, storyId],
  );
  const active = useRef<StoryCopy | null>(copy);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<{ copy: StoryCopy; message: string } | null>(null);
  const [, refresh] = useState(0);
  useEffect(() => {
    active.current = copy;
    setBusy(false);
    const changed = () => refresh((value) => value + 1);
    window.addEventListener('storage', changed);
    return () => {
      active.current = null;
      window.removeEventListener('storage', changed);
    };
  }, [copy]);

  let pending: ReturnType<StoryCopy['pending']> = null;
  let storageError: string | null = null;
  try {
    pending = copy.pending();
  } catch (error) {
    storageError = errorMessage(error);
  }
  const error = storageError ?? (failure?.copy === copy ? failure.message : null);
  const create = async () => {
    if (busy || storageError) return;
    setBusy(true);
    setFailure(null);
    try {
      const projectId = await copy.copy(pending?.idempotency_key ?? null);
      if (active.current !== copy) return;
      await navigate({
        to: '/studio/projects/$projectId/$workspace',
        params: { projectId, workspace: 'edit' },
      });
      copy.opened(projectId);
    } catch (error) {
      if (active.current === copy) setFailure({ copy, message: errorMessage(error) });
    } finally {
      if (active.current === copy) {
        setBusy(false);
        refresh((value) => value + 1);
      }
    }
  };
  return (
    <div className="flex max-w-xs flex-col items-end gap-1">
      <Button
        type="button"
        variant="ghost"
        className="h-7 gap-1.5 px-2 text-xs"
        disabled={busy || !!storageError}
        title="Open an independent Studio project with this Story’s pinned audio, trims and timing"
        onClick={() => void create()}
      >
        <Copy className="h-3.5 w-3.5" aria-hidden="true" />
        {busy
          ? 'Creating studio copy…'
          : pending?.project_id
            ? 'Open studio copy'
            : pending
              ? 'Retry studio copy'
              : 'Create studio copy'}
      </Button>
      {pending && !pending.project_id && !busy && (
        <p className="text-right text-xs text-muted-foreground">
          A previous copy needs acknowledgement.
        </p>
      )}
      {error && (
        <p role="alert" className="text-right text-xs text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
