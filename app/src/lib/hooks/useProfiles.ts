import { currentServerScope, isCurrentServer, requireCurrentServer, type ServerScope } from '@/lib/serverScope';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiClient } from '@/lib/api/client';
import type { EffectConfig, VoiceProfileCreate, VoiceProfileResponse } from '@/lib/api/types';
import { usePlatform } from '@/platform/PlatformContext';
import { useServerStore } from '@/stores/serverStore';

export const profileListKey = (scope: ServerScope) => ['profiles', { kind: 'list', ...scope }] as const;
export const profileDetailKey = (profileId: string, scope: ServerScope) => ['profiles', profileId, 'detail', scope] as const;

function useProfileServerScope(): ServerScope {
  const serverUrl = useServerStore((state) => state.serverUrl);
  const serverRevision = useServerStore((state) => state.serverRevision);
  return { serverUrl, serverRevision };
}

export function useProfiles() {
  const scope = useProfileServerScope();
  return useQuery({
    queryKey: profileListKey(scope),
    queryFn: () => { requireCurrentServer(scope); return apiClient.listProfiles(); },
  });
}

export function useProfile(profileId: string) {
  const scope = useProfileServerScope();
  return useQuery({
    queryKey: profileDetailKey(profileId, scope),
    queryFn: () => { requireCurrentServer(scope); return apiClient.getProfile(profileId); },
    enabled: !!profileId,
  });
}

export function useCreateProfile() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ _serverScope, ...data }: VoiceProfileCreate & { _serverScope?: ServerScope }) => {
      requireCurrentServer(_serverScope);
      return apiClient.createProfile(data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['profiles'] });
    },
  });
}

export function useUpdateProfile() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ profileId, data, _serverScope }: { profileId: string; data: VoiceProfileCreate; _serverScope?: ServerScope }) => {
      requireCurrentServer(_serverScope);
      return apiClient.updateProfile(profileId, data);
    },
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['profiles'] });
      queryClient.invalidateQueries({
        queryKey: ['profiles', variables.profileId],
      });
    },
  });
}

export function useDeleteProfile() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (value: string | { profileId: string; _serverScope: ServerScope }) => {
      if (typeof value === 'string') return apiClient.deleteProfile(value);
      requireCurrentServer(value._serverScope);
      return apiClient.deleteProfile(value.profileId);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['profiles'] });
      // Deleting a profile also deletes its generations, which story items
      // reference — both caches still hold rows that no longer exist.
      queryClient.invalidateQueries({ queryKey: ['history'] });
      queryClient.invalidateQueries({ queryKey: ['stories'] });
    },
  });
}

export function useUpdateProfileEffects() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      profileId,
      effectsChain,
      _serverScope,
    }: {
      profileId: string;
      effectsChain: EffectConfig[] | null;
      _serverScope?: ServerScope;
    }) => {
      requireCurrentServer(_serverScope);
      return apiClient.updateProfileEffects(profileId, effectsChain);
    },
    onSuccess: async (profile, variables) => {
      if (variables._serverScope && !isCurrentServer(variables._serverScope)) return;
      // A metadata save may already have refetched the old effects. Cancel older
      // requests before publishing the effects acknowledgement to both views.
      await queryClient.cancelQueries({ queryKey: ['profiles'] });
      if (variables._serverScope && !isCurrentServer(variables._serverScope)) return;
      const scope = variables._serverScope ?? currentServerScope();
      queryClient.setQueryData(profileDetailKey(profile.id, scope), profile);
      queryClient.setQueryData<VoiceProfileResponse[]>(profileListKey(scope), (previous) =>
        previous?.map((item) => (item.id === profile.id ? profile : item)),
      );
    },
  });
}

export function useProfileSamples(profileId: string) {
  const scope = useProfileServerScope();
  return useQuery({
    queryKey: ['profiles', profileId, 'samples', scope.serverUrl, scope.serverRevision],
    queryFn: () => {
      requireCurrentServer(scope);
      return apiClient.listProfileSamples(profileId);
    },
    enabled: !!profileId,
  });
}

export function useAddSample() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      profileId,
      file,
      referenceText,
      _serverScope,
    }: {
      profileId: string;
      file: File;
      referenceText: string;
      _serverScope?: ServerScope;
    }) => {
      requireCurrentServer(_serverScope);
      return apiClient.addProfileSample(profileId, file, referenceText);
    },
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({
        queryKey: ['profiles', variables.profileId, 'samples'],
      });
      queryClient.invalidateQueries({
        queryKey: ['profiles', variables.profileId],
      });
    },
  });
}

export function useDeleteSample() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (value: string | { sampleId: string; _serverScope: ServerScope }) => {
      if (typeof value === 'string') return apiClient.deleteProfileSample(value);
      requireCurrentServer(value._serverScope);
      return apiClient.deleteProfileSample(value.sampleId);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['profiles'] });
    },
  });
}

export function useUpdateSample() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ sampleId, referenceText, _serverScope }: { sampleId: string; referenceText: string; _serverScope?: ServerScope }) => {
      requireCurrentServer(_serverScope);
      return apiClient.updateProfileSample(sampleId, referenceText);
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({
        queryKey: ['profiles', data.profile_id, 'samples'],
      });
      queryClient.invalidateQueries({
        queryKey: ['profiles', data.profile_id],
      });
      queryClient.invalidateQueries({ queryKey: ['profiles'] });
    },
  });
}

export function useExportSampleAudio() {
  const platform = usePlatform();

  return useMutation({
    mutationFn: async ({
      sampleId,
      referenceText,
      format,
      _serverScope,
    }: {
      sampleId: string;
      referenceText: string;
      format: 'mp3' | 'wav';
      _serverScope?: ServerScope;
    }) => {
      requireCurrentServer(_serverScope);
      const blob = await apiClient.exportSampleAudio(sampleId, format);
      const safeText = referenceText
        .substring(0, 30)
        .replace(/[^a-z0-9]/gi, '-')
        .toLowerCase();
      await platform.filesystem.saveFile(
        `sample-${safeText || 'recording'}-${sampleId.substring(0, 8)}.${format}`,
        blob,
        [{ name: 'Audio File', extensions: [format] }],
      );
    },
  });
}

export function useExportProfile() {
  const platform = usePlatform();

  return useMutation({
    mutationFn: async (value: string | { profileId: string; _serverScope: ServerScope }) => {
      const profileId = typeof value === 'string' ? value : value.profileId;
      const scope = typeof value === 'string' ? currentServerScope() : value._serverScope;
      requireCurrentServer(scope);
      const blob = await apiClient.exportProfile(profileId);

      // Do not fetch metadata from a different server for the downloaded bytes.
      requireCurrentServer(scope);
      const profile = await apiClient.getProfile(profileId);
      requireCurrentServer(scope);
      const safeName = profile.name.replace(/[^a-z0-9]/gi, '-').toLowerCase();
      const filename = `profile-${safeName}.voicebox.zip`;

      await platform.filesystem.saveFile(filename, blob, [
        {
          name: 'Voicebox Profile',
          extensions: ['zip'],
        },
      ]);

      return blob;
    },
  });
}

export function useImportProfile() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (file: File) => apiClient.importProfile(file),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['profiles'] });
    },
  });
}

export function useUploadAvatar() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ profileId, file, _serverScope }: { profileId: string; file: File; _serverScope?: ServerScope }) => {
      requireCurrentServer(_serverScope);
      return apiClient.uploadAvatar(profileId, file);
    },
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['profiles'] });
      queryClient.invalidateQueries({
        queryKey: ['profiles', variables.profileId],
      });
    },
  });
}

export function useDeleteAvatar() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (profileId: string) => apiClient.deleteAvatar(profileId),
    onSuccess: (_, profileId) => {
      queryClient.invalidateQueries({ queryKey: ['profiles'] });
      queryClient.invalidateQueries({
        queryKey: ['profiles', profileId],
      });
    },
  });
}
