import type { AudioDevice, PlatformAudio } from '@/platform/types';

export const webAudio: PlatformAudio = {
  async isSystemAudioSupported(): Promise<boolean> {
    return false; // System audio capture not supported in web
  },

  async startSystemAudioCapture(_maxDurationSecs: number): Promise<void> {
    throw new Error('System audio capture is only available in the desktop app.');
  },

  async stopSystemAudioCapture(): Promise<Blob> {
    throw new Error('System audio capture is only available in the desktop app.');
  },

  async listOutputDevices(): Promise<AudioDevice[]> {
    return []; // No native device routing in web
  },

  async playToDevices() {
    throw new Error('Native audio device routing is only available in the desktop app.');
  },

  async controlPlayback(): Promise<void> {
    throw new Error('Native audio device routing is only available in the desktop app.');
  },

  async subscribeToPlayback(): Promise<() => void> {
    return () => {};
  },

  async stopPlayback(): Promise<void> {
    // No native playback exists in the web app.
  },
};
