import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import type {
  AudioDevice,
  NativePlaybackEvent,
  NativePlaybackSession,
  PlatformAudio,
} from '@/platform/types';

export const tauriAudio: PlatformAudio = {
  async isSystemAudioSupported(): Promise<boolean> {
    return await invoke<boolean>('is_system_audio_supported');
  },

  async startSystemAudioCapture(maxDurationSecs: number): Promise<void> {
    await invoke('start_system_audio_capture', {
      maxDurationSecs,
    });
  },

  async stopSystemAudioCapture(): Promise<Blob> {
    const base64Data = await invoke<string>('stop_system_audio_capture');

    // Convert base64 to Blob
    const binaryString = atob(base64Data);
    const bytes = new Uint8Array(binaryString.length);
    for (let i = 0; i < binaryString.length; i++) {
      bytes[i] = binaryString.charCodeAt(i);
    }

    return new Blob([bytes], { type: 'audio/wav' });
  },

  async listOutputDevices(): Promise<AudioDevice[]> {
    return await invoke<AudioDevice[]>('list_audio_output_devices');
  },

  async playToDevices(audioData, deviceIds, options): Promise<NativePlaybackSession> {
    // Headers require ASCII on every webview transport. JSON Unicode escapes
    // preserve device identifiers without expanding the audio byte buffer.
    const metadata = JSON.stringify({ deviceIds: [...new Set(deviceIds)], options }).replace(
      /[\u007f-\uffff]/g,
      (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`,
    );
    return await invoke('play_audio_to_devices', audioData, {
      headers: {
        'x-voicebox-playback': metadata,
      },
    });
  },

  async controlPlayback(sessionId, control): Promise<void> {
    await invoke('control_audio_playback', { sessionId, control });
  },

  async subscribeToPlayback(callback): Promise<() => void> {
    return await listen<NativePlaybackEvent>('native-audio-playback', ({ payload }) =>
      callback(payload),
    );
  },

  async stopPlayback(sessionId): Promise<void> {
    await invoke('stop_audio_playback', { sessionId });
  },
};
