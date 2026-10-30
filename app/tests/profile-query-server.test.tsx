import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const lists: Array<(value: any[]) => void> = [];
const details: Array<(value: any) => void> = [];
const apiClient = {
  listProfiles: mock(() => new Promise<any[]>((resolve) => lists.push(resolve))),
  getProfile: mock(() => new Promise<any>((resolve) => details.push(resolve))),
  updateProfileEffects: mock(async () => ({ id: 'voice', name: 'Updated', effects_chain: [] })),
  importProfile: mock(async (_file: File) => ({ id: 'imported' })),
};
mock.module('../src/lib/api/client', () => ({ apiClient }));
mock.module('../src/platform/PlatformContext', () => ({ usePlatform: () => ({}) }));
const { useServerStore } = await import('../src/stores/serverStore');
const { currentServerScope } = await import('../src/lib/serverScope');
const { useProfiles, useProfile, useUpdateProfileEffects, useImportProfile } = await import('../src/lib/hooks/useProfiles');
let list: ReturnType<typeof useProfiles>;
let detail: ReturnType<typeof useProfile>;
let effects: ReturnType<typeof useUpdateProfileEffects>;
let importProfile: ReturnType<typeof useImportProfile>;
let view: ReactTestRenderer;
let client: QueryClient;
function Probe() { list = useProfiles(); detail = useProfile('voice'); effects = useUpdateProfileEffects(); importProfile = useImportProfile(); return null; }
const flush = async () => { await new Promise((resolve) => setTimeout(resolve, 20)); };
async function mount() {
  useServerStore.setState({ serverUrl: 'http://server-a', serverRevision: 0 });
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  await act(async () => { view = create(<QueryClientProvider client={client}><Probe /></QueryClientProvider>); await flush(); });
}
afterEach(() => { if (view) act(() => view.unmount()); client?.clear(); lists.length = 0; details.length = 0; });
afterAll(() => mock.restore());

test('server switch hides the old list and detail while new data loads', async () => {
  await mount();
  await act(async () => { lists[0]([{ id: 'voice', name: 'Old' }]); details[0]({ id: 'voice', name: 'Old' }); await flush(); });
  expect(list.data?.[0].name).toBe('Old');
  expect(detail.data?.name).toBe('Old');
  await act(async () => { useServerStore.setState({ serverUrl: 'http://server-b', serverRevision: 1 }); await flush(); });
  expect(list.data).toBeUndefined();
  expect(detail.data).toBeUndefined();
  expect(lists).toHaveLength(2);
  expect(details).toHaveLength(2);
  await act(async () => { lists[1]([{ id: 'voice', name: 'New' }]); details[1]({ id: 'voice', name: 'New' }); await flush(); });
  expect(list.data?.[0].name).toBe('New');
  expect(detail.data?.name).toBe('New');
});

test('late old-server profile responses cannot replace the new server', async () => {
  await mount();
  await act(async () => { useServerStore.setState({ serverUrl: 'http://server-b', serverRevision: 1 }); await flush(); });
  expect(lists).toHaveLength(2);
  expect(details).toHaveLength(2);
  await act(async () => { lists[1]([{ id: 'voice', name: 'New' }]); details[1]({ id: 'voice', name: 'New' }); await flush(); });
  await act(async () => { lists[0]([{ id: 'voice', name: 'Old' }]); details[0]({ id: 'voice', name: 'Old' }); await flush(); });
  expect(list.data?.[0].name).toBe('New');
  expect(detail.data?.name).toBe('New');
});

test('effects acknowledgement updates the visible list and detail', async () => {
  await mount();
  await act(async () => { lists[0]([{ id: 'voice', name: 'Old' }]); details[0]({ id: 'voice', name: 'Old' }); await flush(); });
  await act(async () => { await effects.mutateAsync({ profileId: 'voice', effectsChain: [], _serverScope: currentServerScope() }); await flush(); });
  expect(list.data?.[0].name).toBe('Updated');
  expect(detail.data?.name).toBe('Updated');
});


test('queued profile import cannot dispatch after the selected server changes', async () => {
  await mount();
  apiClient.importProfile.mockClear();
  const scope = currentServerScope();
  const file = new File(['voice'], 'voice.voicebox.zip');
  await act(async () => {
    const pending = importProfile.mutateAsync({ file, _serverScope: scope } as never);
    useServerStore.setState({ serverUrl: 'http://server-b', serverRevision: 1 });
    await expect(pending).rejects.toThrow('Server connection changed');
  });
  expect(apiClient.importProfile).not.toHaveBeenCalled();
});

test('old-server import completion does not invalidate the current profiles cache', async () => {
  await mount();
  let finish!: (value: { id: string }) => void;
  apiClient.importProfile.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  const scope = currentServerScope();
  const file = new File(['voice'], 'voice.voicebox.zip');
  const key = ['profiles', 'inactive'];
  client.setQueryData(key, ['current']);
  let pending!: Promise<unknown>;
  await act(async () => {
    pending = importProfile.mutateAsync({ file, _serverScope: scope } as never);
    await flush();
  });
  await act(async () => {
    useServerStore.setState({ serverUrl: 'http://server-b', serverRevision: 1 });
    finish({ id: 'imported' });
    await pending;
  });
  expect(client.getQueryState(key)?.isInvalidated).toBe(false);
});

test('current-server profile import sends the file and invalidates profiles', async () => {
  await mount();
  apiClient.importProfile.mockClear();
  const file = new File(['voice'], 'voice.voicebox.zip');
  const key = ['profiles', 'inactive'];
  client.setQueryData(key, ['old']);
  await act(async () => {
    await importProfile.mutateAsync({ file, _serverScope: currentServerScope() });
  });
  expect(apiClient.importProfile).toHaveBeenCalledWith(file);
  expect(client.getQueryState(key)?.isInvalidated).toBe(true);
});
