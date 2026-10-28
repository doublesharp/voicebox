import { create } from 'zustand';
import { persist } from 'zustand/middleware';

interface MicrophoneStore {
  deviceId: string | null;
  deviceLabel: string | null;
  setMicrophone: (deviceId: string | null, deviceLabel: string | null) => void;
}

const storageKey = 'voicebox-microphone';

export const useMicrophoneStore = create<MicrophoneStore>()(
  persist(
    (set) => ({
      deviceId: null,
      deviceLabel: null,
      setMicrophone: (deviceId, deviceLabel) => set({ deviceId, deviceLabel }),
    }),
    {
      name: storageKey,
      partialize: ({ deviceId, deviceLabel }) => ({ deviceId, deviceLabel }),
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
