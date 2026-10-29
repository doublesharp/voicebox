import { afterAll, afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const entries = new Map<string, string>();
const oldStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
const oldWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
const oldProd = process.env.PROD;
Object.defineProperty(globalThis, 'localStorage', {
  configurable: true,
  value: {
    getItem: (key: string) => entries.get(key) ?? null,
    setItem: (key: string, value: string) => entries.set(key, value),
    removeItem: (key: string) => entries.delete(key),
  },
});
Object.defineProperty(globalThis, 'window', {
  configurable: true,
  value: { location: new URL('tauri://localhost') },
});
process.env.PROD = 'true';
let finishStartup: (url: string) => void;
let startup: Promise<string>;
const platform = {
  metadata: { isTauri: true },
  lifecycle: {
    setKeepServerRunning: async () => {},
    setupWindowCloseHandler: async () => {},
    subscribeToServerLogs: () => () => {},
    startServer: () => startup,
  },
};
mock.module('../src/platform/PlatformContext', () => ({ usePlatform: () => platform }));
mock.module('../src/hooks/useThemeSync', () => ({ useThemeSync() {} }));
mock.module('../src/hooks/useAutoUpdater', () => ({ useAutoUpdater() {} }));
mock.module('../src/lib/hooks/useChordSync', () => ({ useChordSync() {} }));
mock.module('../src/components/DictateWindow/DictateWindow', () => ({ DictateWindow: () => null }));
mock.module('../src/components/StartupScreen', () => ({ StartupScreen: () => null }));
mock.module('../src/components/ShinyText', () => ({ default: () => null }));
mock.module('../src/router', () => ({ router: {} }));
mock.module('@tanstack/react-router', () => ({ RouterProvider: () => null }));
mock.module('../src/stores/logStore', () => ({
  useLogStore: { getState: () => ({ addEntry() {} }) },
}));
const { useServerStore } = await import('../src/stores/serverStore');
const { default: App } = await import('../src/App');
let tree: ReactTestRenderer | undefined;
beforeEach(() => {
  entries.clear();
  useServerStore.setState(useServerStore.getInitialState(), true);
  platform.metadata.isTauri = true;
  startup = new Promise((resolve) => {
    finishStartup = resolve;
  });
});
afterEach(() => {
  act(() => tree?.unmount());
  tree = undefined;
});
afterAll(() => {
  mock.restore();
  if (oldStorage) Object.defineProperty(globalThis, 'localStorage', oldStorage);
  else Reflect.deleteProperty(globalThis, 'localStorage');
  if (oldWindow) Object.defineProperty(globalThis, 'window', oldWindow);
  else Reflect.deleteProperty(globalThis, 'window');
  if (oldProd === undefined) delete process.env.PROD;
  else process.env.PROD = oldProd;
});

test('desktop startup preserves a saved remote address', async () => {
  useServerStore.getState().setServerUrl('https://voicebox.example.com');
  act(() => {
    tree = create(<App />);
  });
  await act(async () => {
    finishStartup('http://127.0.0.1:19000');
    await startup;
  });
  expect(useServerStore.getState().serverUrl).toBe('https://voicebox.example.com');
});

test('desktop startup respects an address selected while discovery is pending', async () => {
  act(() => {
    tree = create(<App />);
  });
  useServerStore.getState().setServerUrl('http://127.0.0.1:18000');
  await act(async () => {
    finishStartup('http://127.0.0.1:19000');
    await startup;
  });
  expect(useServerStore.getState().serverUrl).toBe('http://127.0.0.1:18000');
});

test('automatic desktop startup adopts its discovered port', async () => {
  act(() => {
    tree = create(<App />);
  });
  await act(async () => {
    finishStartup('http://127.0.0.1:19000');
    await startup;
  });
  expect(useServerStore.getState().serverUrl).toBe('http://127.0.0.1:19000');
});

test('web startup preserves an explicitly selected loopback proxy URL', () => {
  platform.metadata.isTauri = false;
  useServerStore.getState().setServerUrl('http://127.0.0.1:17493/proxy');
  act(() => {
    tree = create(<App />);
  });
  expect(useServerStore.getState().serverUrl).toBe('http://127.0.0.1:17493/proxy');
});
