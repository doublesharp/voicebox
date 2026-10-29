import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { useCallback } from 'react';
import { apiClient } from '@/lib/api/client';
import type {
  CaptureSettings,
  CaptureSettingsUpdate,
  GenerationSettings,
  GenerationSettingsUpdate,
} from '@/lib/api/types';

const CAPTURE_SETTINGS_KEY = ['settings', 'captures'] as const;
const GENERATION_SETTINGS_KEY = ['settings', 'generation'] as const;

type GenerationPending = { patch: GenerationSettingsUpdate };
type GenerationWrites = { confirmed: GenerationSettings | undefined; pending: GenerationPending[] };
const generationWrites = new WeakMap<QueryClient, GenerationWrites>();

function renderGenerationWrites(client: QueryClient, writes: GenerationWrites) {
  if (!writes.confirmed) return;
  client.setQueryData<GenerationSettings>(
    GENERATION_SETTINGS_KEY,
    writes.pending.reduce(
      (settings, pending) => ({ ...settings, ...pending.patch }),
      writes.confirmed,
    ),
  );
}

type CapturePending = { patch: Partial<CaptureSettings> };
type CaptureWrites = { confirmed: CaptureSettings | undefined; pending: CapturePending[] };
const captureWrites = new WeakMap<QueryClient, CaptureWrites>();
type CaptureMutation = {
  patch: Omit<CaptureSettingsUpdate, 'custom_llm_api_key'>;
  secretId?: number;
};
const captureSecrets = new WeakMap<QueryClient, Map<number, string | null>>();
let nextCaptureSecretId = 0;

function renderCaptureWrites(client: QueryClient, writes: CaptureWrites) {
  if (!writes.confirmed) return;
  client.setQueryData<CaptureSettings>(
    CAPTURE_SETTINGS_KEY,
    writes.pending.reduce(
      (settings, pending) => ({ ...settings, ...pending.patch }),
      writes.confirmed,
    ),
  );
}

/**
 * Hook for capture/refine defaults. Reads from the server and writes partial
 * updates with optimistic cache mutation so toggles stay snappy while the
 * PUT round-trip settles.
 */
export function useCaptureSettings(options?: { onError?: () => void }) {
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: CAPTURE_SETTINGS_KEY,
    queryFn: () => apiClient.getCaptureSettings(),
    staleTime: Infinity,
  });

  const mutation = useMutation({
    scope: { id: 'capture-settings' },
    mutationFn: async ({ patch, secretId }: CaptureMutation) => {
      const secrets = captureSecrets.get(queryClient);
      try {
        if (secretId !== undefined && !secrets?.has(secretId)) {
          throw new Error('The pending API key is unavailable');
        }
        return await apiClient.updateCaptureSettings(
          secretId === undefined ? patch : { ...patch, custom_llm_api_key: secrets?.get(secretId) },
        );
      } finally {
        if (secretId !== undefined) secrets?.delete(secretId);
      }
    },
    onMutate: async ({ patch }: CaptureMutation) => {
      await queryClient.cancelQueries({ queryKey: CAPTURE_SETTINGS_KEY });
      let writes = captureWrites.get(queryClient);
      if (!writes) {
        writes = {
          confirmed: queryClient.getQueryData<CaptureSettings>(CAPTURE_SETTINGS_KEY),
          pending: [],
        };
        captureWrites.set(queryClient, writes);
      }
      const pending: CapturePending = { patch };
      writes.pending.push(pending);
      renderCaptureWrites(queryClient, writes);
      return { writes, pending };
    },
    onError: () => options?.onError?.(),
    onSettled: (data, _err, { patch, secretId }, context) => {
      if (secretId !== undefined) captureSecrets.get(queryClient)?.delete(secretId);
      if (context) {
        if (data) context.writes.confirmed = data;
        context.writes.pending = context.writes.pending.filter((item) => item !== context.pending);
        renderCaptureWrites(queryClient, context.writes);
        if (context.writes.pending.length === 0) captureWrites.delete(queryClient);
      }
      // /capture/readiness resolves stt_model / llm_model live on each
      // call, but its cached response keeps serving the previous
      // model's state until the next 5 s poll. Invalidate on model
      // swaps so the readiness checklist re-checks immediately.
      if (
        patch.stt_model !== undefined ||
        patch.llm_model !== undefined ||
        patch.auto_refine !== undefined ||
        patch.custom_llm_endpoint !== undefined ||
        patch.custom_llm_model !== undefined
      ) {
        queryClient.invalidateQueries({ queryKey: ['capture-readiness'] });
      }
    },
  });

  const update = useCallback(
    (patch: CaptureSettingsUpdate) => {
      const { custom_llm_api_key: secret, ...safePatch } = patch;
      let secretId: number | undefined;
      if (secret !== undefined) {
        let secrets = captureSecrets.get(queryClient);
        if (!secrets) {
          secrets = new Map();
          captureSecrets.set(queryClient, secrets);
        }
        secretId = ++nextCaptureSecretId;
        secrets.set(secretId, secret);
      }
      mutation.mutate({ patch: safePatch, secretId });
    },
    [mutation.mutate, queryClient],
  );

  return {
    settings: query.data,
    isLoading: query.isLoading,
    update,
  };
}

/**
 * Hook for long-form TTS generation defaults. Same optimistic pattern as
 * ``useCaptureSettings``.
 */
export function useGenerationSettings(options?: { onError?: () => void }) {
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: GENERATION_SETTINGS_KEY,
    queryFn: () => apiClient.getGenerationSettings(),
    staleTime: Infinity,
  });

  const mutation = useMutation({
    scope: { id: 'generation-settings' },
    mutationFn: (patch: GenerationSettingsUpdate) => apiClient.updateGenerationSettings(patch),
    onMutate: async (patch) => {
      await queryClient.cancelQueries({ queryKey: GENERATION_SETTINGS_KEY });
      let writes = generationWrites.get(queryClient);
      if (!writes) {
        writes = {
          confirmed: queryClient.getQueryData<GenerationSettings>(GENERATION_SETTINGS_KEY),
          pending: [],
        };
        generationWrites.set(queryClient, writes);
      }
      const pending: GenerationPending = { patch };
      writes.pending.push(pending);
      renderGenerationWrites(queryClient, writes);
      return { writes, pending };
    },
    onError: () => options?.onError?.(),
    onSettled: (data, _error, _patch, context) => {
      if (!context) return;
      if (data) context.writes.confirmed = data;
      context.writes.pending = context.writes.pending.filter((item) => item !== context.pending);
      renderGenerationWrites(queryClient, context.writes);
      if (context.writes.pending.length === 0) generationWrites.delete(queryClient);
    },
  });

  return {
    settings: query.data,
    isLoading: query.isLoading,
    update: mutation.mutate,
  };
}
