import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { notifyManager, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const initial = { items: [{ client_id: 'a', label: 'A' }, { client_id: 'b', label: 'B' }] };
const pending: Array<{ id: string; resolve: (value: any) => void; reject: (error: Error) => void }> = [];
const load = mock((): Promise<any> => new Promise(() => {}));
mock.module('../src/lib/api/client', () => ({ apiClient: {
  listMCPBindings: load,
  deleteMCPBinding: (id: string) => new Promise((resolve, reject) => pending.push({ id, resolve, reject })),
  upsertMCPBinding: async () => ({}),
} }));
const { useMCPBindings } = await import('../src/lib/hooks/useMCPBindings');
notifyManager.setScheduler((fn) => queueMicrotask(fn));
let hook: ReturnType<typeof useMCPBindings>;
let client: QueryClient;
let view: ReactTestRenderer;
function Probe() { hook = useMCPBindings(); return null; }
async function flush() { await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); }); }
async function mount() {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } });
  client.setQueryData(['settings', 'mcp', 'bindings'], initial);
  await act(async () => { view = create(<QueryClientProvider client={client}><Probe /></QueryClientProvider>); });
}
afterEach(() => { if (view) act(() => view.unmount()); client?.clear(); pending.length = 0; load.mockReset(); load.mockImplementation(() => new Promise(() => {})); });
afterAll(() => { mock.restore(); notifyManager.setScheduler((fn) => setTimeout(fn, 0)); });

test('failed removal does not overwrite a newer binding update', async () => {
  await mount();
  act(() => hook.remove('a'));
  await flush();
  client.setQueryData(['settings', 'mcp', 'bindings'], { items: [{ client_id: 'b', label: 'Updated B' }] });
  await act(async () => pending[0].reject(new Error('delete failed')));
  await flush();
  expect(client.getQueryData(['settings', 'mcp', 'bindings'])).toEqual({ items: [{ client_id: 'b', label: 'Updated B' }] });
});


test('successful removal keeps the binding visible until the server confirms it', async () => {
  await mount();
  load.mockImplementation(async () => ({ items: [{ client_id: 'b', label: 'B' }] }));
  act(() => hook.remove('a'));
  await flush();
  expect(client.getQueryData(['settings', 'mcp', 'bindings'])).toEqual(initial);
  await act(async () => pending[0].resolve({ deleted: 'a' }));
  await flush();
  expect(client.getQueryData(['settings', 'mcp', 'bindings'])).toEqual({ items: [{ client_id: 'b', label: 'B' }] });
});
