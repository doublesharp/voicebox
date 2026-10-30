import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const handlers = new Map<string, (event: any) => void>();
const emissions: string[] = [];
const invocations: unknown[] = [];
let finalText: (text: string, capture: unknown, allow: boolean) => Promise<void>;
let pasteError: Error | null = null;
const startRecording = mock(() => {});
const stopRecording = mock(() => {});
const restartRecording = mock(() => {});
const sources: FakeSource[] = [];
const audios: FakeAudio[] = [];
class FakeSource {
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  close = mock(() => {});
  constructor(public url: string) {
    sources.push(this);
  }
}
class FakeAudio {
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onplaying: (() => void) | null = null;
  pause = mock(() => {});
  play = mock(() => Promise.resolve());
  constructor(public src: string) {
    audios.push(this);
  }
}
const oldWindow = globalThis.window;
const oldDocument = globalThis.document;
const oldSource = globalThis.EventSource;
const oldAudio = globalThis.Audio;
Object.assign(globalThis, {
  window: globalThis,
  document: {
    documentElement: { style: { background: 'html' } },
    body: { style: { background: 'body' } },
  },
  EventSource: FakeSource,
  Audio: FakeAudio,
});
mock.module('@tauri-apps/api/event', () => ({
  listen: async (name: string, handler: (event: any) => void) => {
    handlers.set(name, handler);
    return () => handlers.delete(name);
  },
  emit: async (name: string) => {
    emissions.push(name);
  },
}));
mock.module('@tauri-apps/api/core', () => ({
  invoke: async (...args: unknown[]) => {
    invocations.push(args);
    if (pasteError) throw pasteError;
  },
}));
mock.module('../src/lib/api/client', () => ({
  apiClient: {
    getAudioUrl: (id: string) => `/audio/${id}`,
    getGenerationStatusUrl: (id: string) => `/status/${id}`,
  },
}));
mock.module('../src/lib/hooks/useCaptureRecordingSession', () => ({
  useCaptureRecordingSession: (options: any) => {
    finalText = options.onFinalText;
    return {
      pillState: 'hidden',
      isRecording: false,
      startRecording,
      stopRecording,
      restartRecording,
      dismissError() {},
    };
  },
}));
mock.module('../src/components/CapturePill/CapturePill', () => ({
  CapturePill: ({ state }: { state: string }) => <div>{state}</div>,
}));
const { DictateWindow } = await import('../src/components/DictateWindow/DictateWindow');
let view: ReactTestRenderer | undefined;
async function mount() {
  await act(async () => {
    view = create(<DictateWindow />);
  });
}
function event(name: string, payload: unknown) {
  act(() => handlers.get(name)?.({ payload }));
}
function complete(source: FakeSource) {
  act(() => source.onmessage?.({ data: '{"status":"completed"}' }));
}
afterEach(async () => {
  await act(async () => view?.unmount());
  view = undefined;
  handlers.clear();
  sources.length = 0;
  audios.length = 0;
  emissions.length = 0;
  invocations.length = 0;
  pasteError = null;
  startRecording.mockClear();
  stopRecording.mockClear();
  restartRecording.mockClear();
});
afterAll(() => {
  mock.restore();
  Object.assign(globalThis, {
    window: oldWindow,
    document: oldDocument,
    EventSource: oldSource,
    Audio: oldAudio,
  });
});

test('superseded generation cannot start playback from a late status event', async () => {
  await mount();
  event('dictate:speak-start', JSON.stringify({ generation_id: 'old' }));
  const old = sources[0];
  event('dictate:speak-start', JSON.stringify({ generation_id: 'new' }));
  expect(old.close).toHaveBeenCalled();
  complete(old);
  expect(audios).toHaveLength(0);
  complete(sources[1]);
  expect(audios.map((audio) => audio.src)).toEqual(['/audio/new']);
  act(() => audios[0].onplaying?.());
  expect(emissions).toContain('dictate:show');
  act(() => audios[0].onended?.());
  expect(emissions).toContain('dictate:hide');
});

test('paste consumes focus once and reports accessibility denial', async () => {
  await mount();
  event('dictate:start', { focus: { app: 'target' } });
  pasteError = new Error('Accessibility permission denied');
  await act(async () => finalText('hello', null, true));
  expect(invocations).toHaveLength(1);
  expect(emissions).toContain('system:accessibility-missing');
  await act(async () => finalText('again', null, true));
  expect(invocations).toHaveLength(1);
  event('dictate:start', { focus: { app: 'target' } });
  await act(async () => finalText('no paste', null, false));
  expect(invocations).toHaveLength(1);
});

test('retired playback cannot show the window and unmount closes the active stream', async () => {
  await mount();
  event('dictate:speak-start', JSON.stringify({ generation_id: 'old' }));
  complete(sources[0]);
  const oldAudio = audios[0];
  event('dictate:speak-start', JSON.stringify({ generation_id: 'new' }));
  expect(oldAudio.pause).toHaveBeenCalledTimes(1);
  emissions.length = 0;
  act(() => oldAudio.onplaying?.());
  expect(emissions).not.toContain('dictate:show');
  const active = sources[1];
  await act(async () => view?.unmount());
  view = undefined;
  expect(active.close).toHaveBeenCalledTimes(1);
  expect(handlers.size).toBe(0);
  expect(document.body.style.background).toBe('body');
});

test('failed generation dismisses without creating audio', async () => {
  await mount();
  event('dictate:speak-start', JSON.stringify({ generation_id: 'failed' }));
  act(() => sources[0].onmessage?.({ data: '{"status":"failed"}' }));
  expect(audios).toHaveLength(0);
  expect(emissions).toContain('dictate:hide');
});


test('hands-free upgrade restarts the dictation and keeps the original focus', async () => {
  await mount();
  const focus = { pid: 42, bundle_id: 'other.app', role: 'AXTextField' };
  event('dictate:start', { focus });
  event('dictate:restart', null);
  expect(startRecording).toHaveBeenCalledTimes(1);
  expect(restartRecording).toHaveBeenCalledTimes(1);
  await act(async () => finalText('hello', null, true));
  expect(invocations[0]).toEqual(['paste_final_text', { text: 'hello', focus }]);
});

test('stop is delivered even while microphone acquisition is pending', async () => {
  await mount();
  event('dictate:start', { focus: null });
  event('dictate:stop', null);
  expect(stopRecording).toHaveBeenCalledTimes(1);
});
