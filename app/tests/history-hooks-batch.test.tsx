import { afterAll, afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { notifyManager, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { currentServerScope } from '../src/lib/serverScope';
import { useServerStore } from '../src/stores/serverStore';

const api = {
  listHistory: mock(async (_query?: any) => [{ id: 'g1' }]),
  getGeneration: mock(async (id: string) => ({ id })),
  deleteGeneration: mock(async (_id: string) => ({})),
  clearFailedGenerations: mock(async () => ({})),
  importGeneration: mock(async (_file: File) => ({})),
  exportGeneration: mock(async (_id: string) => new Blob(['package'])),
  exportGenerationAudio: mock(async (_id: string, _format: string) => new Blob(['audio'])),
};
mock.module('../src/lib/api/client', () => ({ apiClient: api }));
const saveFile = mock(async (_name: string, _blob: Blob, _filters: any) => ({ status: 'saved' }));
mock.module('../src/platform/PlatformContext', () => ({ usePlatform: () => ({ filesystem: { saveFile } }) }));
const H = await import('../src/lib/hooks/useHistory');
notifyManager.setScheduler((fn) => queueMicrotask(fn));
let view: ReactTestRenderer | undefined; let client: QueryClient; let hooks: ReturnType<typeof useAll>;
function useAll(id: string) { return { history: H.useHistory({ profile_id: 'p' }), detail: H.useGenerationDetail(id), remove: H.useDeleteGeneration(), clear: H.useClearFailedGenerations(), load: H.useImportGeneration(), exportPackage: H.useExportGeneration(), exportAudio: H.useExportGenerationAudio() }; }
function Probe({ id }: { id: string }) { hooks = useAll(id); return null; }
async function mount(id = '') { client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } }); await act(async () => { view = create(<QueryClientProvider client={client}><Probe id={id} /></QueryClientProvider>); }); }
async function flush() { await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); }); }
beforeEach(() => { Object.values(api).forEach((m) => m.mockClear()); saveFile.mockClear(); });
afterEach(() => { if (view) act(() => view?.unmount()); view = undefined; client?.clear(); useServerStore.setState({ serverUrl: 'http://server-a', serverRevision: 0 }); });
afterAll(() => { mock.restore(); notifyManager.setScheduler((fn) => setTimeout(fn, 0)); });

test('history passes filters and skips an empty detail id', async () => { await mount(); await flush(); expect(api.listHistory).toHaveBeenCalledWith({ profile_id: 'p' }); expect(api.getGeneration).not.toHaveBeenCalled(); expect(client.getQueryData(['history', { profile_id: 'p' }])).toEqual([{ id: 'g1' }]); });
test('history detail fetches when a generation id becomes available', async () => { await mount(); await act(async () => view!.update(<QueryClientProvider client={client}><Probe id="g1" /></QueryClientProvider>)); await flush(); expect(api.getGeneration).toHaveBeenCalledWith('g1'); expect(client.getQueryData(['history', 'g1'])).toEqual({ id: 'g1' }); });

test.each(['remove', 'clear', 'load'] as const)('%s invalidates history only on success', async (kind) => {
  await mount(); const key = ['history', 'inactive']; client.setQueryData(key, { value: 1 });
  const call = () => kind === 'remove' ? hooks.remove.mutateAsync('g1') : kind === 'clear' ? hooks.clear.mutateAsync() : hooks.load.mutateAsync(new File(['archive'], 'generation.zip'));
  await act(async () => { await call(); }); expect(client.getQueryState(key)?.isInvalidated).toBe(true);
  client.setQueryData(key, { value: 2 });
  const endpoint = kind === 'remove' ? api.deleteGeneration : kind === 'clear' ? api.clearFailedGenerations : api.importGeneration;
  endpoint.mockImplementationOnce(async () => { throw new Error('offline'); });
  await act(async () => { await expect(call()).rejects.toThrow('offline'); }); expect(client.getQueryState(key)?.isInvalidated).toBe(false);
});

test('package export uses safe text and stable id suffix', async () => { await mount(); let blob: Blob | undefined; await act(async () => { blob = await hooks.exportPackage.mutateAsync({ generationId: '12345678-abc', text: 'Hello / World?' }); }); expect(api.exportGeneration).toHaveBeenCalledWith('12345678-abc'); expect(saveFile).toHaveBeenCalledWith('generation-hello---world--12345678.voicebox.zip', blob, [{ name: 'Voicebox Generation', extensions: ['zip'] }]); });
test.each(['wav', 'mp3'] as const)('audio export requests and saves %s', async (format) => { await mount(); let blob: Blob | undefined; await act(async () => { blob = await hooks.exportAudio.mutateAsync({ generationId: 'abcdefgh-1', text: 'A sample', format }); }); expect(api.exportGenerationAudio).toHaveBeenCalledWith('abcdefgh-1', format); expect(saveFile).toHaveBeenCalledWith(`a-sample-abcdefgh.${format}`, blob, [{ name: 'Audio File', extensions: [format] }]); });
test('audio export defaults to wav and propagates save failures', async () => { await mount(); saveFile.mockImplementationOnce(async () => { throw new Error('save failed'); }); await act(async () => { await expect(hooks.exportAudio.mutateAsync({ generationId: 'abc', text: 'Sample' })).rejects.toThrow('save failed'); }); expect(api.exportGenerationAudio).toHaveBeenCalledWith('abc', 'wav'); });
test('failed export never invokes the save adapter', async () => { await mount(); api.exportGeneration.mockImplementationOnce(async () => { throw new Error('server failed'); }); await act(async () => { await expect(hooks.exportPackage.mutateAsync({ generationId: 'g1', text: 'Sample' })).rejects.toThrow('server failed'); }); expect(saveFile).not.toHaveBeenCalled(); });

test.each(['remove', 'clear'] as const)('queued %s cannot dispatch after a server change', async (kind) => {
  useServerStore.setState({ serverUrl: 'http://server-a', serverRevision: 0 });
  await mount();
  const scope = currentServerScope();
  let pending!: Promise<unknown>;
  await act(async () => {
    pending = kind === 'remove'
      ? hooks.remove.mutateAsync({ generationId: 'g1', scope })
      : hooks.clear.mutateAsync(scope);
    useServerStore.setState({ serverUrl: 'http://server-b', serverRevision: 1 });
    await expect(pending).rejects.toThrow('Server connection changed');
  });
  expect(api.deleteGeneration).not.toHaveBeenCalled();
  expect(api.clearFailedGenerations).not.toHaveBeenCalled();
});
