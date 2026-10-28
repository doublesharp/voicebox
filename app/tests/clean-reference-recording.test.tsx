import { afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { PlatformProvider } from '../src/platform/PlatformContext';
import type { Platform } from '../src/platform/types';

const stored = new Map<string, string>();
const storage = {
  getItem: (key: string) => stored.get(key) ?? null,
  setItem: (key: string, value: string) => stored.set(key, value),
  removeItem: (key: string) => stored.delete(key),
};
stored.set(
  'voicebox-reference-capture',
  JSON.stringify({ state: { cleanReferenceAudio: true }, version: 0 }),
);
Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage });
Object.defineProperty(globalThis, 'window', {
  configurable: true,
  value: Object.assign(new EventTarget(), { setInterval, clearInterval, localStorage: storage }),
});
mock.module('../src/lib/utils/audio', () => ({ convertToWav: async (blob: Blob) => blob }));
const { useReferenceCaptureStore } = await import('../src/stores/referenceCaptureStore');
const { useMicrophoneStore } = await import('../src/stores/microphoneStore');
const restoredPreference = useMicrophoneStore.getState().cleanReferenceAudio;
const { useAudioRecording } = await import('../src/lib/hooks/useAudioRecording');

class Recorder {
  static isTypeSupported() {
    return false;
  }
  state = 'inactive';
  mimeType = 'audio/mp4';
  onstop: (() => void) | null = null;
  start() {
    this.state = 'recording';
  }
  stop() {
    this.state = 'inactive';
    queueMicrotask(() => this.onstop?.());
  }
}
Object.defineProperty(globalThis, 'MediaRecorder', { configurable: true, value: Recorder });

let renderer: ReactTestRenderer | null = null;
let recording!: ReturnType<typeof useAudioRecording>;
let completed: Blob | null = null;
function Probe({ processing }: { processing?: 'processed' | 'clean' }) {
  recording = useAudioRecording({
    processing,
    onRecordingComplete: (blob) => {
      completed = blob;
    },
  });
  return null;
}
function mount(processing?: 'processed' | 'clean') {
  act(() => {
    renderer = create(
      <PlatformProvider platform={{ metadata: { isTauri: false } } as Platform}>
        <Probe processing={processing} />
      </PlatformProvider>,
    );
  });
}
function acquireMicrophone() {
  const stop = mock(() => {});
  const stream = {
    getTracks: () => [{ stop }],
    getAudioTracks: () => [{ getSettings: () => ({}) }],
  } as unknown as MediaStream;
  const acquire = mock(async (_constraints: MediaStreamConstraints) => stream);
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: acquire },
  });
  return { acquire, stop };
}
afterEach(async () => {
  await act(async () => {
    renderer?.unmount();
  });
  renderer = null;
  completed = null;
  useReferenceCaptureStore.getState().setCleanReferenceAudio(false);
});

test('a clean reference requests unprocessed audio and labels the actual fallback container', async () => {
  const { acquire, stop } = acquireMicrophone();
  mount('clean');
  await act(async () => {
    await recording.startRecording();
  });
  expect(acquire.mock.calls[0][0].audio).toEqual({
    echoCancellation: false,
    noiseSuppression: false,
    autoGainControl: false,
  });
  await act(async () => {
    recording.stopRecording();
  });
  expect((completed as Blob | null)?.type).toBe('audio/mp4');
  expect(stop).toHaveBeenCalled();
});

test('dictation keeps processed defaults when the reference preference is enabled', async () => {
  useReferenceCaptureStore.getState().setCleanReferenceAudio(true);
  const { acquire } = acquireMicrophone();
  mount();
  await act(async () => {
    await recording.startRecording();
  });
  expect(acquire.mock.calls[0][0].audio).toEqual({
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  });
});

test('the clean reference preference is persisted independently of microphone selection', () => {
  useReferenceCaptureStore.getState().setCleanReferenceAudio(true);
  expect(useReferenceCaptureStore).toBe(useMicrophoneStore);
  const value = JSON.parse(storage.getItem('voicebox-microphone')!);
  expect(value.state.cleanReferenceAudio).toBe(true);
});

test('the earlier clean reference preference survives consolidation with microphone settings', () => {
  expect(restoredPreference).toBe(true);
});

test('a saved combined preference takes priority over the earlier reference setting', async () => {
  storage.setItem(
    'voicebox-microphone',
    JSON.stringify({ state: { cleanReferenceAudio: false }, version: 0 }),
  );
  await useMicrophoneStore.persist.rehydrate();
  expect(useMicrophoneStore.getState().cleanReferenceAudio).toBe(false);
});
