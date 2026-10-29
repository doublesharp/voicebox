import { afterAll, afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

let request: () => Promise<any> = async () => ({ downloads: [], generations: [] });
mock.module('../src/lib/api/client', () => ({ apiClient: { getActiveTasks: () => request() } }));
const { useRestoreActiveTasks } = await import('../src/lib/hooks/useRestoreActiveTasks');
const { useGenerationStore } = await import('../src/stores/generationStore');
const originalSetInterval = globalThis.setInterval;
const originalClearInterval = globalThis.clearInterval;
let tick: () => void;
let cleared = 0;
let view: ReactTestRenderer | undefined;
let downloads: any[] = [];
function Probe() { downloads = useRestoreActiveTasks(); return null; }
async function mount() { await act(async () => { view = create(<Probe />); await Promise.resolve(); }); }
async function flush() { await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); }); }
const task = (id: string) => ({ task_id: id });
const dl = (name: string) => ({ model_name: name, status: 'downloading' });
beforeEach(() => {
  useGenerationStore.setState({ pendingGenerationIds: new Set(), isGenerating: false, activeGenerationId: null });
  request = async () => ({ downloads: [], generations: [] });
  cleared = 0;
  globalThis.setInterval = ((fn: () => void) => { tick = fn; return 42; }) as any;
  globalThis.clearInterval = (() => { cleared++; }) as any;
});
afterEach(() => { if (view) act(() => view?.unmount()); view = undefined; globalThis.setInterval = originalSetInterval; globalThis.clearInterval = originalClearInterval; });
afterAll(() => mock.restore());

test('restores active generations and download progress, then polls for updates', async () => {
  let response = { generations: [task('first'), task('second')], downloads: [dl('whisper')] };
  request = async () => response;
  await mount(); await flush();
  expect(useGenerationStore.getState().activeGenerationId).toBe('first');
  expect([...useGenerationStore.getState().pendingGenerationIds]).toEqual(['first', 'second']);
  expect(downloads.map((x) => x.model_name)).toEqual(['whisper']);
  response = { generations: [], downloads: [dl('other')] };
  act(() => { tick(); }); await flush();
  expect(useGenerationStore.getState().activeGenerationId).toBeNull();
  expect(downloads.map((x) => x.model_name)).toEqual(['other']);
  act(() => view?.unmount()); view = undefined; expect(cleared).toBe(1);
});

test('failed polling preserves the last successful snapshot', async () => {
  request = async () => ({ generations: [task('live')], downloads: [dl('model')] });
  await mount(); await flush();
  request = async () => { throw new Error('offline'); };
  act(() => { tick(); }); await flush();
  expect(useGenerationStore.getState().activeGenerationId).toBe('live'); expect(downloads.map((x) => x.model_name)).toEqual(['model']);
});

test('older poll cannot overwrite a newer successful snapshot', async () => {
  const finish: Array<(value: any) => void> = [];
  request = () => new Promise((resolve) => finish.push(resolve));
  await mount(); act(() => { tick(); });
  await act(async () => finish[1]({ generations: [task('new')], downloads: [dl('new')] })); await flush();
  await act(async () => finish[0]({ generations: [task('old')], downloads: [dl('old')] })); await flush();
  expect(useGenerationStore.getState().activeGenerationId).toBe('new'); expect(downloads.map((x) => x.model_name)).toEqual(['new']);
  expect(useGenerationStore.getState().pendingGenerationIds.has('old')).toBe(false);
});

test('an unmounted poll cannot modify global generation state', async () => {
  let finish!: (value: any) => void;
  request = () => new Promise((resolve) => { finish = resolve; });
  await mount(); act(() => view?.unmount()); view = undefined;
  await act(async () => finish({ generations: [task('late')], downloads: [dl('late')] })); await flush();
  expect(useGenerationStore.getState().activeGenerationId).toBeNull(); expect(useGenerationStore.getState().pendingGenerationIds.size).toBe(0);
});

test('empty response cannot clear a generation started after the request', async () => {
  let finish!: (value: any) => void;
  request = () => new Promise((resolve) => { finish = resolve; });
  await mount(); act(() => useGenerationStore.getState().setActiveGenerationId('newly-started'));
  await act(async () => finish({ generations: [], downloads: [] })); await flush();
  expect(useGenerationStore.getState().activeGenerationId).toBe('newly-started');
});

test('old active response cannot replace a generation started after the request', async () => {
  let finish!: (value: any) => void;
  request = () => new Promise((resolve) => { finish = resolve; });
  await mount(); act(() => useGenerationStore.getState().setActiveGenerationId('newly-started'));
  await act(async () => finish({ generations: [task('older')], downloads: [] })); await flush();
  expect(useGenerationStore.getState().activeGenerationId).toBe('newly-started');
});
