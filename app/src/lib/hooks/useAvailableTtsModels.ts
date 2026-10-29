import { useEffect, useMemo, useState } from 'react';
import { apiClient } from '@/lib/api/client';
import { useServerStore } from '@/stores/serverStore';

/** Discover optional variants from the connected server, independent of UI OS. */
export function useAvailableTtsModels() {
  const serverUrl = useServerStore((state) => state.serverUrl);
  const [known, setKnown] = useState<{ serverUrl: string; names: string[] } | null>(null);
  useEffect(() => {
    let active = true;
    void apiClient.getModelStatus().then(
      (response) => {
        if (active) setKnown({ serverUrl, names: response.models.map((model) => model.model_name) });
      },
      () => {
        if (active) setKnown({ serverUrl, names: [] });
      },
    );
    return () => { active = false; };
  }, [serverUrl]);
  return useMemo(() => ({
    names: new Set(known?.serverUrl === serverUrl ? known.names : []),
    loading: known?.serverUrl !== serverUrl,
  }), [known, serverUrl]);
}
