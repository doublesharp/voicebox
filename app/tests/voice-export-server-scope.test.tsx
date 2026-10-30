import { afterAll, afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const pending: Array<{ resolve: (value: Blob) => void; reject: (error: Error) => void }> = [];
const exports = mock((_id: string) => new Promise<Blob>((resolve, reject) => pending.push({ resolve, reject })));
const profile = mock(async (_id: string) => ({ id: 'voice', name: 'Current Server Voice' }));
const save = mock(async (_name: string, _blob: Blob) => {});
mock.module('../src/lib/api/client', () => ({ apiClient: { exportProfile: exports, getProfile: profile } }));
mock.module('../src/platform/PlatformContext', () => ({ usePlatform: () => ({ filesystem: { saveFile: save } }) }));
const { useServerStore } = await import('../src/stores/serverStore');
const { useExportProfile } = await import('../src/lib/hooks/useProfiles');
let view: ReactTestRenderer;
let client: QueryClient;
let hook: ReturnType<typeof useExportProfile>;
function Probe() { hook = useExportProfile(); return null; }
async function mount() {
  client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  await act(async () => { view = create(<QueryClientProvider client={client}><Probe /></QueryClientProvider>); });
}
beforeEach(() => { pending.length = 0; exports.mockClear(); profile.mockClear(); save.mockClear(); useServerStore.setState({ serverUrl: 'http://server-a', serverRevision: 0 }); });
afterEach(() => { if (view) act(() => view.unmount()); client?.clear(); });
afterAll(() => mock.restore());

test('a server switch during export does not read a different profile for its filename', async () => {
  await mount();
  let result!: Promise<unknown>;
  await act(async () => { result = hook.mutateAsync('voice').then(() => null, (error) => error); await Promise.resolve(); });
  expect(pending).toHaveLength(1);
  act(() => useServerStore.setState({ serverUrl: 'http://server-b', serverRevision: 1 }));
  await act(async () => { pending[0].resolve(new Blob(['old-server-voice'])); await result; });
  expect(profile).not.toHaveBeenCalled();
  expect(save).not.toHaveBeenCalled();
  expect(await result).toBeInstanceOf(Error);
});

test('a normal export keeps its bytes and profile name', async () => {
  await mount();
  let result!: Promise<unknown>;
  await act(async () => { result = hook.mutateAsync('voice'); await Promise.resolve(); });
  const blob = new Blob(['voice data']);
  await act(async () => { pending[0].resolve(blob); await result; });
  expect(profile).toHaveBeenCalledWith('voice');
  expect(save).toHaveBeenCalledWith('profile-current-server-voice.voicebox.zip', blob, expect.any(Array));
});

test('queued export cannot start on a newly selected server', async () => {
  await mount();
  const scope = { serverUrl: 'http://server-a', serverRevision: 0 };
  let result!: Promise<unknown>;
  act(() => {
    result = hook.mutateAsync({ profileId: 'voice', _serverScope: scope }).then(() => null, (error) => error);
    useServerStore.setState({ serverUrl: 'http://server-b', serverRevision: 1 });
  });
  await act(async () => { await result; });
  expect(await result).toBeInstanceOf(Error);
  expect(exports).not.toHaveBeenCalled();
  expect(save).not.toHaveBeenCalled();
});

test('switching away and back during metadata lookup does not save a stale export', async () => {
  await mount();
  let resolveProfile!: (value: { id: string; name: string }) => void;
  profile.mockImplementationOnce(() => new Promise((resolve) => { resolveProfile = resolve; }));
  let result!: Promise<unknown>;
  await act(async () => { result = hook.mutateAsync('voice').then(() => null, (error) => error); await Promise.resolve(); });
  await act(async () => { pending[0].resolve(new Blob(['voice'])); await Promise.resolve(); });
  expect(profile).toHaveBeenCalledTimes(1);
  act(() => {
    useServerStore.setState({ serverUrl: 'http://server-b', serverRevision: 1 });
    useServerStore.setState({ serverUrl: 'http://server-a', serverRevision: 2 });
  });
  await act(async () => { resolveProfile({ id: 'voice', name: 'old' }); await result; });
  expect(await result).toBeInstanceOf(Error);
  expect(save).not.toHaveBeenCalled();
});

test('failed export does not try to fetch a name or save a file', async () => {
  await mount();
  let result!: Promise<unknown>;
  await act(async () => { result = hook.mutateAsync('voice').then(() => null, (error) => error); await Promise.resolve(); });
  await act(async () => { pending[0].reject(new Error('network failed')); await result; });
  expect((await result as Error).message).toBe('network failed');
  expect(profile).not.toHaveBeenCalled();
  expect(save).not.toHaveBeenCalled();
});
