import { useEffect, useState } from 'react';
import type { StudioClient } from './client';
import type { StudioDocument, StudioRevision } from './types';

/** Fetch only the immutable checkpoint currently being reviewed or delivered. */
export function useRevisionDocument(
  client: StudioClient,
  projectId: string,
  revision: StudioRevision | undefined,
  enabled = true,
) {
  const [result, setResult] = useState<{
    key: string;
    document?: StudioDocument;
    error?: string;
  }>({ key: '' });
  const revisionId = revision?.id;
  const key = `${client.baseUrl}:${projectId}:${revisionId ?? ''}`;
  const inline = revision?.document;
  useEffect(() => {
    if (!enabled || !revisionId || inline) return;
    const request = new AbortController();
    client.revision(projectId, revisionId, request.signal).then(
      (detail) => {
        if (!request.signal.aborted) {
          setResult(
            detail.document
              ? { key, document: detail.document }
              : { key, error: 'Checkpoint document is unavailable.' },
          );
        }
      },
      (error: unknown) => {
        if (!request.signal.aborted)
          setResult({ key, error: error instanceof Error ? error.message : String(error) });
      },
    );
    return () => request.abort();
  }, [client, projectId, revisionId, inline, enabled, key]);
  return {
    document: inline ?? (result.key === key ? result.document : undefined),
    error: result.key === key ? result.error : undefined,
  };
}
