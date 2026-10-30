import { expect, mock, test } from 'bun:test';

type CloseCallback = (event: { payload: string }) => Promise<void>;
let callback: CloseCallback | undefined;
let keepRunning = true;
const emitted: Array<[string, unknown]> = [];
const invocations: string[] = [];
let stop: () => Promise<void> = async () => {};

mock.module('@tauri-apps/api/core', () => ({
  invoke: async (command: string) => { invocations.push(command); await stop(); },
}));
mock.module('@tauri-apps/api/event', () => ({
  listen: async (_name: string, handler: CloseCallback) => { callback = handler; return () => {}; },
  emit: async (name: string, payload?: unknown) => { emitted.push([name, payload]); },
}));
mock.module('@/stores/serverStore', () => ({
  useServerStore: { getState: () => ({ keepServerRunningOnClose: keepRunning }) },
}));
Object.assign(globalThis, { window: { __voiceboxServerStartedByApp: true } });
const { tauriLifecycle } = await import('../src/platform/lifecycle');

test('close handler echoes the request ID after cleanup', async () => {
  keepRunning = false;
  emitted.length = 0;
  invocations.length = 0;
  await tauriLifecycle.setupWindowCloseHandler();
  await callback!({ payload: '9007199254740993' });
  expect(invocations).toEqual(['stop_server']);
  expect(emitted).toEqual([['window-close-allowed', '9007199254740993']]);
});


test('keep-running close echoes the ID without stopping the server', async () => {
  keepRunning = true;
  emitted.length = 0;
  invocations.length = 0;
  await callback!({ payload: '5' });
  expect(invocations).toEqual([]);
  expect(emitted).toEqual([['window-close-allowed', '5']]);
});

test('failed server cleanup still echoes its own ID', async () => {
  keepRunning = false;
  emitted.length = 0;
  stop = async () => { throw new Error('stop failed'); };
  await callback!({ payload: '9' });
  stop = async () => {};
  expect(emitted).toEqual([['window-close-allowed', '9']]);
});

test('overlapping cleanups acknowledge their own IDs in completion order', async () => {
  keepRunning = false;
  emitted.length = 0;
  const releases: Array<() => void> = [];
  stop = () => new Promise<void>((resolve) => { releases.push(resolve); });
  const first = callback!({ payload: '13' });
  const second = callback!({ payload: '17' });
  while (releases.length < 2) await new Promise((resolve) => setTimeout(resolve, 0));
  releases[1]();
  await second;
  releases[0]();
  await first;
  stop = async () => {};
  expect(emitted).toEqual([['window-close-allowed', '17'], ['window-close-allowed', '13']]);
});

test('malformed close IDs do not start cleanup or acknowledge', async () => {
  keepRunning = false;
  emitted.length = 0;
  invocations.length = 0;
  for (const payload of [null, 1, '', '1x', '-1']) {
    await callback!({ payload: payload as unknown as string });
  }
  expect(invocations).toEqual([]);
  expect(emitted).toEqual([]);
});
