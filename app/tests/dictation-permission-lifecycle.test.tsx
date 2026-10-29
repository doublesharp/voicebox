import { afterAll, afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

let isTauri = true;
let invokeImpl: (command: string) => Promise<any> = async () => true;
let listenImpl: (handler: () => void) => Promise<() => void> = async () => () => {};
const invoke = mock((command: string) => invokeImpl(command));
const listen = mock((_event: string, handler: () => void) => listenImpl(handler));
mock.module('@tauri-apps/api/core', () => ({ invoke }));
mock.module('@tauri-apps/api/event', () => ({ listen }));
mock.module('../src/platform/PlatformContext', () => ({ usePlatform: () => ({ metadata: { isTauri } }) }));
const { useAccessibilityPermission } = await import('../src/components/AccessibilityGate/AccessibilityGate');
const { useInputMonitoringPermission } = await import('../src/components/InputMonitoringGate/InputMonitoringGate');
const oldWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
const events = new EventTarget();
Object.defineProperty(globalThis, 'window', { configurable: true, value: events });
let view: ReactTestRenderer | undefined;
let state: ReturnType<typeof useAccessibilityPermission>;
function Probe({ kind }: { kind: 'accessibility' | 'input' }) { state = kind === 'accessibility' ? useAccessibilityPermission() : useInputMonitoringPermission(); return null; }
async function mount(kind: 'accessibility' | 'input') { await act(async () => { view = create(<Probe kind={kind} />); }); }
async function flush() { await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); }); }
beforeEach(() => { isTauri = true; invoke.mockClear(); listen.mockClear(); invokeImpl = async () => true; listenImpl = async () => () => {}; });
afterEach(() => { if (view) act(() => view?.unmount()); view = undefined; });
afterAll(() => { mock.restore(); if (oldWindow) Object.defineProperty(globalThis, 'window', oldWindow); else Reflect.deleteProperty(globalThis, 'window'); });

test.each(['accessibility', 'input'] as const)('%s reports missing permission and opens settings', async (kind) => {
  invokeImpl = async (command) => command.startsWith('check_') ? false : undefined;
  await mount(kind); await flush();
  expect(state.needsPermission).toBe(true);
  await act(async () => state.openSettings());
  expect(invoke.mock.calls.some(([command]) => command === `open_${kind === 'input' ? 'input_monitoring' : 'accessibility'}_settings`)).toBe(true);
});

test.each(['accessibility', 'input'] as const)('%s ignores older results after a newer permission check', async (kind) => {
  const requests: Array<(value: boolean) => void> = [];
  invokeImpl = () => new Promise((resolve) => requests.push(resolve));
  await mount(kind);
  act(() => events.dispatchEvent(new Event('focus')));
  expect(requests).toHaveLength(2);
  await act(async () => requests[1](true)); await flush();
  expect(state.needsPermission).toBe(false);
  await act(async () => requests[0](false)); await flush();
  expect(state.needsPermission).toBe(false);
});

test.each(['accessibility', 'input'] as const)('%s keeps checking until its newest request settles', async (kind) => {
  const requests: Array<(value: boolean) => void> = [];
  invokeImpl = () => new Promise((resolve) => requests.push(resolve));
  await mount(kind);
  act(() => events.dispatchEvent(new Event('focus')));
  await act(async () => requests[0](true)); await flush();
  expect(state.checking).toBe(true);
  await act(async () => requests[1](true)); await flush();
  expect(state.checking).toBe(false);
});

test('a late accessibility listener is immediately removed after unmount', async () => {
  let finish!: (unlisten: () => void) => void;
  const unlisten = mock(() => {});
  listenImpl = () => new Promise((resolve) => { finish = resolve; });
  await mount('accessibility');
  act(() => view?.unmount()); view = undefined;
  await act(async () => finish(unlisten)); await flush();
  expect(unlisten).toHaveBeenCalledTimes(1);
});

test('an accessibility missing event wins over an older in-flight check', async () => {
  let finish!: (value: boolean) => void;
  let event!: () => void;
  invokeImpl = () => new Promise((resolve) => { finish = resolve; });
  listenImpl = async (handler) => { event = handler; return () => {}; };
  await mount('accessibility'); await flush();
  act(() => event());
  expect(state.needsPermission).toBe(true);
  await act(async () => finish(true)); await flush();
  expect(state.needsPermission).toBe(true);
});

test.each(['accessibility', 'input'] as const)('%s skips native calls outside Tauri', async (kind) => {
  isTauri = false;
  await mount(kind); await flush();
  expect(invoke).not.toHaveBeenCalled();
  expect(listen).not.toHaveBeenCalled();
  await expect(state.recheck()).resolves.toBe(true);
});
