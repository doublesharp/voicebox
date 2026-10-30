import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const handlers = new Map<string, (event: { payload: unknown }) => void>();
mock.module('@tauri-apps/api/event', () => ({
  listen: async (name: string, fn: (event: { payload: unknown }) => void) => {
    handlers.set(name, fn);
    return () => handlers.delete(name);
  },
  emit: async () => {},
}));
mock.module('@tauri-apps/api/core', () => ({ invoke: async () => {} }));
mock.module('../src/platform/PlatformContext', () => ({ usePlatform: () => ({ metadata: { isTauri: true } }) }));
mock.module('../src/stores/microphoneStore', () => ({
  useMicrophoneStore: Object.assign((selector: (state: { deviceId: null }) => unknown) => selector({ deviceId: null }), {
    getState: () => ({ deviceId: null }),
  }),
}));
mock.module('../src/lib/utils/audio', () => ({ convertToWav: async (blob: Blob) => blob }));
const createCapture = mock(async () => { throw new Error('cancelled recording must not upload'); });
mock.module('../src/lib/api/client', () => ({ apiClient: { createCapture } }));
mock.module('../src/components/CapturePill/CapturePill', () => ({ CapturePill: () => null }));
const { DictateWindow } = await import('../src/components/DictateWindow/DictateWindow');

const descriptors = new Map(['window', 'document', 'navigator', 'MediaRecorder'].map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
Object.defineProperty(globalThis, 'window', { configurable: true, value: globalThis });
Object.defineProperty(globalThis, 'document', { configurable: true, value: { documentElement: { style: {} }, body: { style: {} } } });
const recorders: FakeRecorder[] = [];
class FakeRecorder {
  static isTypeSupported() { return true; }
  state = 'inactive';
  mimeType = 'audio/webm';
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() { recorders.push(this); }
  start() { this.state = 'recording'; }
  stop() { this.state = 'inactive'; this.onstop?.(); }
}
Object.defineProperty(globalThis, 'MediaRecorder', { configurable: true, value: FakeRecorder });
let tree: ReactTestRenderer | undefined;
let client: QueryClient | undefined;
afterEach(() => {
  act(() => tree?.unmount());
  tree = undefined;
  client?.clear();
  handlers.clear();
  recorders.length = 0;
  createCapture.mockClear();
});
afterAll(() => {
  mock.restore();
  for (const [name, descriptor] of descriptors) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else Reflect.deleteProperty(globalThis, name);
  }
});

test('hands-free restart replaces a pending microphone request without publishing the old session', async () => {
  let resolveOld!: (stream: MediaStream) => void;
  const oldStream = new Promise<MediaStream>((resolve) => { resolveOld = resolve; });
  const oldTrack = { stop: mock(() => {}), getSettings: () => ({ deviceId: 'old' }) };
  const newTrack = { stop: mock(() => {}), getSettings: () => ({ deviceId: 'new' }) };
  let requests = 0;
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { mediaDevices: {
    getUserMedia: async () => {
      requests++;
      return requests === 1 ? oldStream : { getTracks: () => [newTrack], getAudioTracks: () => [newTrack] };
    },
  } } });
  client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  await act(async () => { tree = create(<QueryClientProvider client={client!}><DictateWindow /></QueryClientProvider>); });
  await act(async () => { handlers.get('dictate:start')?.({ payload: { focus: null } }); });
  expect(requests).toBe(1);
  await act(async () => { handlers.get('dictate:restart')?.({ payload: null }); });
  expect(requests).toBe(2);
  expect(recorders).toHaveLength(1);
  expect(recorders[0].state).toBe('recording');
  await act(async () => {
    resolveOld({ getTracks: () => [oldTrack], getAudioTracks: () => [oldTrack] } as unknown as MediaStream);
    await Promise.resolve();
  });
  expect(oldTrack.stop).toHaveBeenCalledTimes(1);
  expect(newTrack.stop).not.toHaveBeenCalled();
  expect(recorders[0].state).toBe('recording');
  expect(createCapture).not.toHaveBeenCalled();
});
