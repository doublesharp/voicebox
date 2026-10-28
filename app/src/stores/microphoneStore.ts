import { create } from 'zustand';
import { persist } from 'zustand/middleware';

interface MicrophoneStore {
  deviceId: string | null;
  deviceLabel: string | null;
  cleanReferenceAudio: boolean;
  setMicrophone: (deviceId: string | null, deviceLabel: string | null) => void;
  setCleanReferenceAudio: (enabled: boolean) => void;
}

const storageKey = 'voicebox-microphone';

function earlierCleanReferencePreference(): boolean {
  try {
    const saved = JSON.parse(localStorage.getItem('voicebox-reference-capture') ?? 'null');
    return saved?.state?.cleanReferenceAudio === true;
  } catch {
    return false;
  }
}

export const useMicrophoneStore = create<MicrophoneStore>()(
  persist(
    (set) => ({
      deviceId: null,
      deviceLabel: null,
      cleanReferenceAudio: earlierCleanReferencePreference(),
      setMicrophone: (deviceId, deviceLabel) => set({ deviceId, deviceLabel }),
      setCleanReferenceAudio: (cleanReferenceAudio) => set({ cleanReferenceAudio }),
    }),
    {
      name: storageKey,
      partialize: ({ deviceId, deviceLabel, cleanReferenceAudio }) => ({
        deviceId,
        deviceLabel,
        cleanReferenceAudio,
      }),
    },
  ),
);

// The dictation pill is a separate webview. Keep its next recording in sync
// with the microphone chosen in the main window.
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (event) => {
    if (event.key === storageKey) void useMicrophoneStore.persist.rehydrate();
  });
}
