import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { notifyManager, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const pending: Array<{ patch: any; resolve: (data: any) => void; reject: (error: Error) => void }> =
  [];
const settings = { language: 'en', auto_refine: true, custom_llm_api_key_configured: false };
mock.module('../src/lib/api/client', () => ({
  apiClient: {
    getCaptureSettings: async () => settings,
    updateCaptureSettings: (patch: any) =>
      new Promise((resolve, reject) => pending.push({ patch, resolve, reject })),
  },
}));
const { useCaptureSettings } = await import('../src/lib/hooks/useSettings');
notifyManager.setScheduler((fn) => queueMicrotask(fn));
let client: QueryClient;
let view: ReactTestRenderer;
let hook: ReturnType<typeof useCaptureSettings>;
function Probe() {
  hook = useCaptureSettings();
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
  client.setQueryData(['settings', 'captures'], settings);
  await act(async () => {
    view = create(
      <QueryClientProvider client={client}>
        <Probe />
      </QueryClientProvider>,
    );
  });
}
afterEach(() => {
  act(() => view?.unmount());
  client?.clear();
  pending.length = 0;
});
afterAll(() => {
  mock.restore();
  notifyManager.setScheduler((fn) => setTimeout(fn, 0));
});

test('a write-only API key never enters the settings query cache', async () => {
  await mount();
  act(() => hook.update({ custom_llm_api_key: 'private-token' }));
  await flush();
  expect(pending).toHaveLength(1);
  expect(pending[0].patch.custom_llm_api_key).toBe('private-token');
  expect(client.getQueryData(['settings', 'captures'])).not.toHaveProperty('custom_llm_api_key');
  expect(JSON.stringify(client.getQueryData(['settings', 'captures']))).not.toContain(
    'private-token',
  );
  expect(
    JSON.stringify(
      client
        .getMutationCache()
        .getAll()
        .map((mutation) => mutation.state.variables),
    ),
  ).not.toContain('private-token');
  await act(async () => pending[0].resolve({ ...settings, custom_llm_api_key_configured: true }));
  await flush();
  expect(client.getQueryData(['settings', 'captures'])).toMatchObject({
    custom_llm_api_key_configured: true,
  });
});

test('a failed earlier update does not erase a later setting', async () => {
  await mount();
  act(() => {
    hook.update({ language: 'fr' });
    hook.update({ auto_refine: false });
  });
  await flush();
  expect(pending).toHaveLength(1);
  expect(client.getQueryData(['settings', 'captures'])).toMatchObject({
    language: 'fr',
    auto_refine: false,
  });
  await act(async () => pending[0].reject(new Error('first update failed')));
  await flush();
  expect(pending).toHaveLength(2);
  expect(client.getQueryData(['settings', 'captures'])).toMatchObject({
    language: 'en',
    auto_refine: false,
  });
  await act(async () => pending[1].resolve({ ...settings, auto_refine: false }));
  await flush();
  expect(client.getQueryData(['settings', 'captures'])).toMatchObject({
    language: 'en',
    auto_refine: false,
  });
});

test('queued edits stay visible and are sent in order after an earlier success', async () => {
  await mount();
  act(() => {
    hook.update({ language: 'fr' });
    hook.update({ language: 'de' });
  });
  await flush();
  expect(pending).toHaveLength(1);
  expect(client.getQueryData(['settings', 'captures'])).toMatchObject({ language: 'de' });
  await act(async () => pending[0].resolve({ ...settings, language: 'fr' }));
  await flush();
  expect(pending).toHaveLength(2);
  expect(pending[1].patch).toEqual({ language: 'de' });
  expect(client.getQueryData(['settings', 'captures'])).toMatchObject({ language: 'de' });
  await act(async () => pending[1].resolve({ ...settings, language: 'de' }));
  await flush();
  expect(client.getQueryData(['settings', 'captures'])).toMatchObject({ language: 'de' });
});

test('clearing the saved key is sent without exposing a key in either cache', async () => {
  await mount();
  act(() => hook.update({ custom_llm_api_key: null }));
  await flush();
  expect(pending[0].patch.custom_llm_api_key).toBeNull();
  expect(client.getQueryData(['settings', 'captures'])).not.toHaveProperty('custom_llm_api_key');
  expect(client.getMutationCache().getAll()[0].state.variables).not.toHaveProperty(
    'custom_llm_api_key',
  );
  await act(async () => pending[0].resolve(settings));
  await flush();
});

test('queued updates complete after the initiating component unmounts', async () => {
  await mount();
  act(() => {
    hook.update({ language: 'fr' });
    hook.update({ auto_refine: false });
  });
  await flush();
  act(() => view.unmount());
  await act(async () => pending[0].resolve({ ...settings, language: 'fr' }));
  await flush();
  expect(pending).toHaveLength(2);
  await act(async () => pending[1].resolve({ ...settings, language: 'fr', auto_refine: false }));
  await flush();
  expect(client.getQueryData(['settings', 'captures'])).toMatchObject({
    language: 'fr',
    auto_refine: false,
  });
});

test('failed key writes leave the confirmed settings and retained mutations free of the key', async () => {
  await mount();
  act(() => hook.update({ custom_llm_api_key: 'failed-token' }));
  await flush();
  await act(async () => pending[0].reject(new Error('save failed')));
  await flush();
  expect(client.getQueryData(['settings', 'captures'])).toMatchObject({
    custom_llm_api_key_configured: false,
  });
  expect(JSON.stringify(client.getQueryData(['settings', 'captures']))).not.toContain(
    'failed-token',
  );
  expect(
    JSON.stringify(
      client
        .getMutationCache()
        .getAll()
        .map((mutation) => mutation.state.variables),
    ),
  ).not.toContain('failed-token');
});
