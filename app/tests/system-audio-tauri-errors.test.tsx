import { afterAll, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
Object.defineProperty(globalThis, 'window', {
  configurable: true,
  value: { setInterval, clearInterval },
});
let failureCommand: string | null = null;
const calls: string[] = [];
mock.module('@tauri-apps/api/core', () => ({
  invoke: async (command: string) => {
    calls.push(command);
    if (command === failureCommand) throw `native ${command} failed`;
    if (command === 'is_system_audio_supported') return true;
    return undefined;
  },
}));
mock.module('@tauri-apps/api/event', () => ({ listen: async () => () => {} }));
const { tauriAudio } = await import('../../tauri/src/platform/audio');
const platform = { metadata: { isTauri: true }, audio: tauriAudio };
mock.module('../src/platform/PlatformContext', () => ({ usePlatform: () => platform }));
const { useSystemAudioCapture } = await import('../src/lib/hooks/useSystemAudioCapture');
let hook!: ReturnType<typeof useSystemAudioCapture>;
function Probe() {
  hook = useSystemAudioCapture();
  return null;
}

afterAll(() => {
  mock.restore();
  if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow);
  else Reflect.deleteProperty(globalThis, 'window');
});

test('Tauri capture errors reach the user through the adapter and hook', async () => {
  let view!: ReactTestRenderer;
  await act(async () => {
    view = create(<Probe />);
  });
  failureCommand = 'start_system_audio_capture';
  await act(async () => {
    await hook.startRecording();
  });
  expect(hook.error).toBe('native start_system_audio_capture failed');
  failureCommand = null;
  await act(async () => {
    await hook.startRecording();
  });
  expect(hook.isRecording).toBe(true);
  failureCommand = 'stop_system_audio_capture';
  await act(async () => {
    await hook.stopRecording();
  });
  expect(hook.error).toBe('native stop_system_audio_capture failed');
  expect(hook.isRecording).toBe(false);
  expect(calls.filter((name) => name === 'start_system_audio_capture')).toHaveLength(2);
  expect(calls.filter((name) => name === 'stop_system_audio_capture')).toHaveLength(1);
  await act(async () => view.unmount());
});
