import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiClient } from '@/lib/api/client';
import type { EffectConfig, VoiceProfileCreate, VoiceProfileResponse } from '@/lib/api/types';
import { usePlatform } from '@/platform/PlatformContext';

export function useProfiles() {
  return useQuery({
    queryKey: ['profiles'],
    queryFn: () => apiClient.listProfiles(),
  });
}

export function useProfile(profileId: string) {
  return useQuery({
    queryKey: ['profiles', profileId],
    queryFn: () => apiClient.getProfile(profileId),
    enabled: !!profileId,
  });
}

export function useCreateProfile() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: VoiceProfileCreate) => apiClient.createProfile(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['profiles'] });
    },
  });
}

export function useUpdateProfile() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ profileId, data }: { profileId: string; data: VoiceProfileCreate }) =>
      apiClient.updateProfile(profileId, data),
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
    mutationFn: (profileId: string) => apiClient.deleteProfile(profileId),
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
    }: {
      profileId: string;
      effectsChain: EffectConfig[] | null;
    }) => apiClient.updateProfileEffects(profileId, effectsChain),
    onSuccess: async (profile) => {
      // A metadata save may already have refetched the old effects. Cancel older
      // requests before publishing the effects acknowledgement to both views.
      await queryClient.cancelQueries({ queryKey: ['profiles'] });
      queryClient.setQueryData(['profiles', profile.id], profile);
      queryClient.setQueryData<VoiceProfileResponse[]>(['profiles'], (previous) =>
        previous?.map((item) => (item.id === profile.id ? profile : item)),
      );
    },
  });
}

export function useProfileSamples(profileId: string) {
  return useQuery({
    queryKey: ['profiles', profileId, 'samples'],
    queryFn: () => apiClient.listProfileSamples(profileId),
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
    }: {
      profileId: string;
      file: File;
      referenceText: string;
    }) => apiClient.addProfileSample(profileId, file, referenceText),
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
    mutationFn: (sampleId: string) => apiClient.deleteProfileSample(sampleId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['profiles'] });
    },
  });
}

export function useUpdateSample() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ sampleId, referenceText }: { sampleId: string; referenceText: string }) =>
      apiClient.updateProfileSample(sampleId, referenceText),
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
    }: {
      sampleId: string;
      referenceText: string;
      format: 'mp3' | 'wav';
    }) => {
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
    mutationFn: async (profileId: string) => {
      const blob = await apiClient.exportProfile(profileId);

      // Get profile name for filename
      const profile = await apiClient.getProfile(profileId);
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
    mutationFn: ({ profileId, file }: { profileId: string; file: File }) =>
      apiClient.uploadAvatar(profileId, file),
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
