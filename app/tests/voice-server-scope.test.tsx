import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const calls: string[] = [];
const apiClient = Object.fromEntries(['createProfile', 'updateProfile', 'deleteProfile', 'addProfileSample', 'uploadAvatar', 'deleteAvatar', 'updateProfileEffects', 'transcribeAudio', 'deleteProfileSample', 'updateProfileSample', 'exportSampleAudio'].map((name) => [name, mock(async (..._args: any[]) => { calls.push(name); return { id: 'voice', text: 'words' }; })]));
mock.module('../src/lib/api/client', () => ({ apiClient }));
mock.module('../src/platform/PlatformContext', () => ({ usePlatform: () => ({ filesystem: { saveFile: async () => {} } }) }));
const { useServerStore } = await import('../src/stores/serverStore');
const { currentServerScope } = await import('../src/lib/serverScope');
const profiles = await import('../src/lib/hooks/useProfiles');
const { useTranscription } = await import('../src/lib/hooks/useTranscription');
let hooks: any;
let view: ReactTestRenderer;
let client: QueryClient;
function Probe() {
  hooks = {
    create: profiles.useCreateProfile(), update: profiles.useUpdateProfile(), delete: profiles.useDeleteProfile(),
    add: profiles.useAddSample(), avatar: profiles.useUploadAvatar(), effects: profiles.useUpdateProfileEffects(),
    transcribe: useTranscription(), deleteSample: profiles.useDeleteSample(),
    updateSample: profiles.useUpdateSample(), exportSample: profiles.useExportSampleAudio(),
    deleteAvatar: profiles.useDeleteAvatar(),
  };
  return null;
}
async function mount() {
  useServerStore.setState({ serverUrl: 'http://server-a', serverRevision: 0 });
  client = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } });
  await act(async () => { view = create(<QueryClientProvider client={client}><Probe /></QueryClientProvider>); });
}
afterEach(() => { if (view) act(() => view.unmount()); client?.clear(); calls.length = 0; });
afterAll(() => mock.restore());

for (const kind of ['create', 'update', 'delete', 'add', 'avatar', 'deleteAvatar', 'effects', 'transcribe', 'deleteSample', 'updateSample', 'exportSample'] as const) {
  test(`queued ${kind} cannot dispatch to a different server`, async () => {
    await mount();
    const scope = currentServerScope();
    const file = new File(['a'], 'voice.wav');
    const args: Record<string, any> = {
      create: { name: 'Voice', _serverScope: scope },
      update: { profileId: 'voice', data: { name: 'Voice' }, _serverScope: scope },
      delete: { profileId: 'voice', _serverScope: scope },
      add: { profileId: 'voice', file, referenceText: 'words', _serverScope: scope },
      avatar: { profileId: 'voice', file, _serverScope: scope },
      deleteAvatar: { profileId: 'voice', _serverScope: scope },
      effects: { profileId: 'voice', effectsChain: [], _serverScope: scope },
      transcribe: { file, _serverScope: scope },
      deleteSample: { sampleId: 'sample', _serverScope: scope },
      updateSample: { sampleId: 'sample', referenceText: 'words', _serverScope: scope },
      exportSample: { sampleId: 'sample', referenceText: 'words', format: 'wav', _serverScope: scope },
    };
    let outcome!: Promise<unknown>;
    act(() => {
      outcome = hooks[kind].mutateAsync(args[kind]).then(() => null, (error: Error) => error);
      useServerStore.setState({ serverUrl: 'http://server-b', serverRevision: 1 });
    });
    const result = await outcome;
    expect(result).toBeInstanceOf(Error);
    expect((result as Error).message).toContain('Server connection changed');
    expect(calls).toEqual([]);
  });
}

test('scope metadata is not sent to the create-profile API', async () => {
  await mount();
  const scope = currentServerScope();
  await act(async () => { await hooks.create.mutateAsync({ name: 'Voice', _serverScope: scope }); });
  expect(apiClient.createProfile).toHaveBeenCalledWith({ name: 'Voice' });
});

test('a previous server effects response cannot replace the current server cache', async () => {
  await mount();
  const scope = currentServerScope();
  let resolve!: (value: any) => void;
  apiClient.updateProfileEffects.mockImplementationOnce(() => new Promise((r) => { resolve = r; }));
  let outcome!: Promise<unknown>;
  await act(async () => {
    outcome = hooks.effects.mutateAsync({ profileId: 'voice', effectsChain: [], _serverScope: scope });
    await new Promise((r) => setTimeout(r, 0));
  });
  act(() => {
    useServerStore.setState({ serverUrl: 'http://server-b', serverRevision: 1 });
    client.setQueryData(profiles.profileDetailKey('voice', currentServerScope()), { id: 'voice', name: 'New server' });
    client.setQueryData(profiles.profileListKey(currentServerScope()), [{ id: 'voice', name: 'New server' }]);
  });
  await act(async () => { resolve({ id: 'voice', name: 'Old server' }); await outcome; });
  expect(client.getQueryData(profiles.profileDetailKey('voice', currentServerScope()))).toEqual({ id: 'voice', name: 'New server' });
  expect(client.getQueryData(profiles.profileListKey(currentServerScope()))).toEqual([{ id: 'voice', name: 'New server' }]);
});
