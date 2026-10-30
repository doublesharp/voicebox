import { requireCurrentServer, type ServerScope } from '@/lib/serverScope';
import { useMutation } from '@tanstack/react-query';
import { apiClient } from '@/lib/api/client';
import type { WhisperModelSize } from '@/lib/api/types';
import type { LanguageCode } from '@/lib/constants/languages';

export function useTranscription() {
  return useMutation({
    mutationFn: ({
      file,
      language,
      model,
      _serverScope,
    }: {
      file: File;
      language?: LanguageCode;
      model?: WhisperModelSize;
      _serverScope?: ServerScope;
    }) => {
      requireCurrentServer(_serverScope);
      return apiClient.transcribeAudio(file, language, model);
    },
  });
}
