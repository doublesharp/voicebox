import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { notifyManager, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const pending: Array<{
  patch: any;
  resolve: (value: any) => void;
  reject: (error: Error) => void;
}> = [];
const initial = {
  max_chunk_chars: 800,
  crossfade_ms: 50,
  normalize_audio: true,
  autoplay_on_generate: false,
};
mock.module('../src/lib/api/client', () => ({
  apiClient: {
    getGenerationSettings: async () => initial,
    updateGenerationSettings: (patch: any) =>
      new Promise((resolve, reject) => pending.push({ patch, resolve, reject })),
  },
}));
const { useGenerationSettings } = await import('../src/lib/hooks/useSettings');
notifyManager.setScheduler((fn) => queueMicrotask(fn));
let view: ReactTestRenderer | undefined;
let client: QueryClient;
let hook: ReturnType<typeof useGenerationSettings>;
function Probe() {
  hook = useGenerationSettings();
  return null;
}
async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}
async function mount() {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } },
  });
  client.setQueryData(['settings', 'generation'], initial);
  await act(async () => {
    view = create(
      <QueryClientProvider client={client}>
        <Probe />
      </QueryClientProvider>,
    );
  });
}
afterEach(() => {
  if (view) act(() => view?.unmount());
  view = undefined;
  client?.clear();
  pending.length = 0;
});
afterAll(() => {
  mock.restore();
  notifyManager.setScheduler((fn) => setTimeout(fn, 0));
});

test('a failed generation setting does not overwrite a later edit', async () => {
  await mount();
  act(() => {
    hook.update({ max_chunk_chars: 1200 });
    hook.update({ normalize_audio: false });
  });
  await flush();
  expect(client.getQueryData(['settings', 'generation'])).toMatchObject({
    max_chunk_chars: 1200,
    normalize_audio: false,
  });
  if (pending.length === 2) {
    await act(async () => pending[1].resolve({ ...initial, normalize_audio: false }));
    await flush();
    await act(async () => pending[0].reject(new Error('first save failed')));
    await flush();
  } else {
    await act(async () => pending[0].reject(new Error('first save failed')));
    await flush();
    expect(pending).toHaveLength(2);
    await act(async () => pending[1].resolve({ ...initial, normalize_audio: false }));
    await flush();
  }
  expect(client.getQueryData(['settings', 'generation'])).toMatchObject({
    max_chunk_chars: 800,
    normalize_audio: false,
  });
});

test('later edits remain visible and persist in order after earlier success', async () => {
  await mount();
  act(() => {
    hook.update({ crossfade_ms: 70 });
    hook.update({ crossfade_ms: 100 });
  });
  await flush();
  expect(pending).toHaveLength(1);
  expect(client.getQueryData(['settings', 'generation'])).toMatchObject({ crossfade_ms: 100 });
  await act(async () => pending[0].resolve({ ...initial, crossfade_ms: 70 }));
  await flush();
  expect(pending).toHaveLength(2);
  expect(client.getQueryData(['settings', 'generation'])).toMatchObject({ crossfade_ms: 100 });
  await act(async () => pending[1].resolve({ ...initial, crossfade_ms: 100 }));
  await flush();
  expect(client.getQueryData(['settings', 'generation'])).toMatchObject({ crossfade_ms: 100 });
});

test('queued generation changes settle after the initiating view unmounts', async () => {
  await mount();
  act(() => {
    hook.update({ max_chunk_chars: 1200 });
    hook.update({ autoplay_on_generate: true });
  });
  await flush();
  act(() => view?.unmount());
  view = undefined;
  await act(async () => pending[0].resolve({ ...initial, max_chunk_chars: 1200 }));
  await flush();
  expect(pending).toHaveLength(2);
  await act(async () =>
    pending[1].resolve({ ...initial, max_chunk_chars: 1200, autoplay_on_generate: true }),
  );
  await flush();
  expect(client.getQueryData(['settings', 'generation'])).toMatchObject({
    max_chunk_chars: 1200,
    autoplay_on_generate: true,
  });
});
