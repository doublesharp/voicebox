import { create } from 'zustand';
import { persist } from 'zustand/middleware';

interface ReferenceCaptureState {
  cleanReferenceAudio: boolean;
  setCleanReferenceAudio: (enabled: boolean) => void;
}

export const useReferenceCaptureStore = create<ReferenceCaptureState>()(
  persist(
    (set) => ({
      cleanReferenceAudio: false,
      setCleanReferenceAudio: (cleanReferenceAudio) => set({ cleanReferenceAudio }),
    }),
    { name: 'voicebox-reference-capture' },
  ),
);
