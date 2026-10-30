import { afterAll, expect, test } from 'bun:test';

const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
const values = new Map<string, string>();
Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
  getItem: (key: string) => values.get(key) ?? null,
  setItem: (key: string, value: string) => values.set(key, value),
  removeItem: (key: string) => values.delete(key),
} });
const { useServerStore } = await import('../src/stores/serverStore');
afterAll(() => { if (original) Object.defineProperty(globalThis, 'localStorage', original); else Reflect.deleteProperty(globalThis, 'localStorage'); });

test('in-flight setting writes are not persisted across restarts', () => {
  useServerStore.setState({ keepServerRunningOnClose: true, keepServerRunningSyncing: true });
  const saved = JSON.parse(values.get('voicebox-server')!);
  expect(saved.state.keepServerRunningOnClose).toBe(true);
  expect(saved.state).not.toHaveProperty('keepServerRunningSyncing');
  useServerStore.setState({ keepServerRunningSyncing: false });
});
