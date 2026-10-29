import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
let serverUrl = 'http://first.invalid';
const requests: Array<{ resolve: (value: any) => void; reject: (error: Error) => void }> = [];
mock.module('../src/stores/serverStore', () => ({
  useServerStore: (select: any) => select({ serverUrl }),
}));
mock.module('../src/lib/api/client', () => ({
  apiClient: { getModelStatus: () => new Promise((resolve, reject) => requests.push({ resolve, reject })) },
}));
const { useAvailableTtsModels } = await import('../src/lib/hooks/useAvailableTtsModels');
let current: ReturnType<typeof useAvailableTtsModels>;
let view: ReactTestRenderer;
function Harness() { current = useAvailableTtsModels(); return null; }
afterEach(() => { if (view) act(() => view.unmount()); requests.length = 0; serverUrl = 'http://first.invalid'; });
afterAll(() => mock.restore());

test('late results from another server cannot advertise unavailable models', async () => {
  await act(async () => { view = create(<Harness />); });
  expect(current!.loading).toBe(true);
  serverUrl = 'http://second.invalid';
  await act(async () => { view.update(<Harness />); });
  await act(async () => { requests[0].resolve({ models: [{ model_name: 'qwen-tts-1.7B-8bit' }] }); });
  expect(current!.loading).toBe(true);
  expect(current!.names.size).toBe(0);
  await act(async () => { requests[1].resolve({ models: [{ model_name: 'qwen-tts-1.7B-4bit' }] }); });
  expect([...current!.names]).toEqual(['qwen-tts-1.7B-4bit']);
  expect(current!.loading).toBe(false);
});

test('discovery failure completes without advertising a guessed precision', async () => {
  await act(async () => { view = create(<Harness />); });
  await act(async () => { requests[0].reject(new Error('offline')); });
  expect(current!.loading).toBe(false);
  expect(current!.names.size).toBe(0);
});
