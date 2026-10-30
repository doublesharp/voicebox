import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const requests: Array<{ resolve: (samples: any[]) => void }> = [];
const listProfileSamples = mock(() => new Promise<any[]>((resolve) => { requests.push({ resolve }); }));
mock.module('../src/lib/api/client', () => ({ apiClient: { listProfileSamples } }));
mock.module('../src/platform/PlatformContext', () => ({ usePlatform: () => ({}) }));
const { useServerStore } = await import('../src/stores/serverStore');
const { useProfileSamples } = await import('../src/lib/hooks/useProfiles');
let result: ReturnType<typeof useProfileSamples>;
let view: ReactTestRenderer;
let client: QueryClient;
function Probe() { result = useProfileSamples('voice'); return null; }
const flush = async () => { await new Promise((resolve) => setTimeout(resolve, 20)); };
async function mount() {
  useServerStore.setState({ serverUrl: 'http://server-a', serverRevision: 0 });
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => { view = create(<QueryClientProvider client={client}><Probe /></QueryClientProvider>); await flush(); });
}
afterEach(() => { if (view) act(() => view.unmount()); client?.clear(); requests.length = 0; listProfileSamples.mockClear(); });
afterAll(() => mock.restore());

test('switching servers hides old samples while the new server loads', async () => {
  await mount();
  await act(async () => { requests[0].resolve([{ id: 'old-sample' }]); await flush(); });
  expect(result.data).toEqual([{ id: 'old-sample' }]);
  await act(async () => { useServerStore.setState({ serverUrl: 'http://server-b', serverRevision: 1 }); await flush(); });
  expect(result.data).toBeUndefined();
  expect(requests).toHaveLength(2);
  await act(async () => { requests[1].resolve([{ id: 'new-sample' }]); await flush(); });
  expect(result.data).toEqual([{ id: 'new-sample' }]);
});

test('a late old-server response cannot replace samples from the new server', async () => {
  await mount();
  await act(async () => { useServerStore.setState({ serverUrl: 'http://server-b', serverRevision: 1 }); await flush(); });
  expect(requests).toHaveLength(2);
  await act(async () => { requests[1].resolve([{ id: 'new-sample' }]); await flush(); });
  await act(async () => { requests[0].resolve([{ id: 'old-sample' }]); await flush(); });
  expect(result.data).toEqual([{ id: 'new-sample' }]);
});
