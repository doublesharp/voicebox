import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { useServerStore } from '../src/stores/serverStore';

const status = mock(async () => ({ connected: false }));
const login = mock(async () => ({}));
const disconnect = mock(async () => ({}));
const toast = mock((_value: any) => {});
mock.module('../src/lib/api/client', () => ({ apiClient: { getCloudStatus: status, startCloudLogin: login, disconnectCloud: disconnect } }));
mock.module('../src/components/ui/use-toast', () => ({ useToast: () => ({ toast }) }));
const { CloudSection } = await import('../src/components/ServerTab/CloudSection');
const oldWindow = globalThis.window;
let timeoutCallback: (() => void) | undefined;
globalThis.window = { setTimeout: (fn: () => void) => { timeoutCallback = fn; return 1; }, clearTimeout: () => { timeoutCallback = undefined; } } as unknown as Window & typeof globalThis;
let view: ReactTestRenderer;
let client: QueryClient;
const flush = async () => { await new Promise((resolve) => setTimeout(resolve, 20)); };
async function mount() {
  useServerStore.setState({ serverUrl: 'http://server-a', serverRevision: 0 });
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  await act(async () => { view = create(<QueryClientProvider client={client}><CloudSection /></QueryClientProvider>); await flush(); });
  await act(flush);
}
afterEach(() => { if (view) act(() => view.unmount()); client?.clear(); toast.mockClear(); status.mockReset(); status.mockImplementation(async () => ({ connected: false })); login.mockReset(); login.mockImplementation(async () => ({})); disconnect.mockReset(); disconnect.mockImplementation(async () => ({})); timeoutCallback = undefined; });
afterAll(() => { globalThis.window = oldWindow; mock.restore(); });
const findButton = (label: string) => view.root.findAllByType('button').find((node) => node.children.includes(label))!;

test('login waits for browser authorization and stops when connected', async () => {
  await mount();
  await act(async () => { findButton('Log in with browser').props.onClick(); await flush(); });
  expect(login).toHaveBeenCalledTimes(1);
  expect(JSON.stringify(view.toJSON())).toContain('Waiting for browser');
  await act(async () => { client.setQueriesData({ queryKey: ['cloud-status'] }, { connected: true, device_name: 'Laptop', key_prefix: 'abc', dashboard_url: 'https://example.com/account' }); await flush(); });
  expect(JSON.stringify(view.toJSON())).toContain('Linked as Laptop');
  expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: 'Connected to Voicebox Cloud' }));
  expect(view.root.findByType('a').props).toMatchObject({ href: 'https://example.com/account', target: '_blank', rel: 'noopener noreferrer' });
});

test('an abandoned sign-in times out and allows another attempt', async () => {
  await mount();
  await act(async () => { findButton('Log in with browser').props.onClick(); await flush(); });
  expect(timeoutCallback).toBeDefined();
  act(() => timeoutCallback!());
  expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: 'Sign-in timed out' }));
  expect(findButton('Log in with browser').props.disabled).toBe(false);
});

test('failed login reports the error and remains available', async () => {
  login.mockImplementationOnce(async () => { throw new Error('backend unavailable'); });
  await mount();
  await act(async () => { findButton('Log in with browser').props.onClick(); await flush(); });
  expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: 'Could not start sign-in', description: 'backend unavailable' }));
  expect(findButton('Log in with browser').props.disabled).toBe(false);
});

test('disconnect reports failure without hiding the connected account', async () => {
  status.mockImplementationOnce(async () => ({ connected: true, device_name: 'Laptop' } as any));
  disconnect.mockImplementationOnce(async () => { throw new Error('cannot disconnect'); });
  await mount();
  await act(async () => { findButton('Disconnect').props.onClick(); await flush(); });
  expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: 'Could not disconnect', description: 'cannot disconnect' }));
  expect(findButton('Disconnect')).toBeDefined();
});

test('late login completion from the previous server does not start polling', async () => {
  let resolve!: (value: object) => void;
  login.mockImplementationOnce(() => new Promise((r) => { resolve = r; }));
  await mount();
  await act(async () => { findButton('Log in with browser').props.onClick(); await flush(); });
  act(() => useServerStore.setState({ serverUrl: 'http://server-b', serverRevision: 1 }));
  await act(async () => { resolve({}); await flush(); });
  expect(JSON.stringify(view.toJSON())).not.toContain('Waiting for browser');
  expect(toast).not.toHaveBeenCalled();
});

test('switching servers hides the previous account while the new status loads', async () => {
  status.mockImplementationOnce(async () => ({ connected: true, device_name: 'Old laptop' } as any));
  let resolve!: (value: { connected: boolean }) => void;
  status.mockImplementationOnce(() => new Promise((r) => { resolve = r; }));
  await mount();
  expect(JSON.stringify(view.toJSON())).toContain('Old laptop');
  await act(async () => { useServerStore.setState({ serverUrl: 'http://server-b', serverRevision: 1 }); await flush(); });
  expect(JSON.stringify(view.toJSON())).not.toContain('Old laptop');
  expect(findButton('Log in with browser')).toBeDefined();
  await act(async () => { resolve({ connected: false }); await flush(); });
});

for (const action of ['login', 'disconnect'] as const) {
  test(`queued cloud ${action} cannot dispatch to the new server`, async () => {
    if (action === 'disconnect') status.mockImplementationOnce(async () => ({ connected: true, device_name: 'Laptop' } as any));
    await mount();
    await act(async () => {
      findButton(action === 'login' ? 'Log in with browser' : 'Disconnect').props.onClick();
      useServerStore.setState({ serverUrl: 'http://server-b', serverRevision: 1 });
      await flush();
    });
    expect(action === 'login' ? login : disconnect).not.toHaveBeenCalled();
    expect(toast).not.toHaveBeenCalled();
  });
}

test('a late timeout from the previous server does not report an error', async () => {
  await mount();
  await act(async () => { findButton('Log in with browser').props.onClick(); await flush(); });
  const oldTimeout = timeoutCallback!;
  toast.mockClear();
  act(() => useServerStore.setState({ serverUrl: 'http://server-b', serverRevision: 1 }));
  act(() => oldTimeout());
  expect(toast).not.toHaveBeenCalled();
});

test('successful disconnect refreshes account status', async () => {
  status.mockImplementationOnce(async () => ({ connected: true, device_name: 'Laptop' } as any));
  await mount();
  await act(async () => { findButton('Disconnect').props.onClick(); await flush(); });
  expect(disconnect).toHaveBeenCalledTimes(1);
  expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: 'Disconnected' }));
  await act(flush);
  expect(findButton('Log in with browser')).toBeDefined();
});
