import { afterAll, afterEach, expect, test } from 'bun:test';
import { notifyManager, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import {
  profileDetailKey,
  profileListKey,
  useDeleteProfile,
  useProfile,
  useProfiles,
  useUpdateProfileEffects,
} from '../src/lib/hooks/useProfiles';
import {
  useDeleteStory,
  useMoveStoryItem,
  useStories,
  useStory,
} from '../src/lib/hooks/useStories';
import { currentServerScope } from '../src/lib/serverScope';
import { useStoryStore } from '../src/stores/storyStore';

notifyManager.setScheduler((callback) => queueMicrotask(callback));
afterAll(() => notifyManager.setScheduler((callback) => setTimeout(callback, 0)));
const originalFetch = globalThis.fetch;
let view: ReactTestRenderer | undefined;
let client: QueryClient;
let hooks: ReturnType<typeof useAll>;
function useAll(profileId: string, storyId: string | null) {
  return {
    profiles: useProfiles(),
    profile: useProfile(profileId),
    stories: useStories(),
    story: useStory(storyId),
    effects: useUpdateProfileEffects(),
    deleteProfile: useDeleteProfile(),
    deleteStory: useDeleteStory(),
    move: useMoveStoryItem(),
  };
}
function Probe({ profileId, storyId }: { profileId: string; storyId: string | null }) {
  hooks = useAll(profileId, storyId);
  return null;
}
async function mount(profileId = '', storyId: string | null = null) {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } },
  });
  await act(async () => {
    view = create(
      <QueryClientProvider client={client}>
        <Probe profileId={profileId} storyId={storyId} />
      </QueryClientProvider>,
    );
  });
}
afterEach(() => {
  act(() => view?.unmount());
  view = undefined;
  client?.clear();
  globalThis.fetch = originalFetch;
  useStoryStore.getState().setSelectedStoryId(null);
});

test('empty detail identifiers do not fetch and enabled queries use distinct keys', async () => {
  const urls: string[] = [];
  globalThis.fetch = (async (url) => {
    urls.push(String(url));
    return Response.json([]);
  }) as typeof fetch;
  await mount();
  expect(urls.map((url) => new URL(url).pathname).sort()).toEqual(['/profiles', '/stories']);
  await act(async () => {
    view?.update(
      <QueryClientProvider client={client}>
        <Probe profileId="p" storyId="s" />
      </QueryClientProvider>,
    );
  });
  expect(urls.map((url) => new URL(url).pathname)).toContain('/profiles/p');
  expect(urls.map((url) => new URL(url).pathname)).toContain('/stories/s');
  expect(client.getQueryData(profileDetailKey('p', currentServerScope()))).toEqual([]);
  expect(client.getQueryData(['stories', 's'])).toEqual([]);
});

test('effects acknowledgement replaces both caches and cancels an older fetch', async () => {
  const old = { id: 'p', name: 'old', effects_chain: [] };
  const updated = { ...old, effects_chain: [{ type: 'gain' }] };
  globalThis.fetch = (async (_url, init) =>
    Response.json(init?.method === 'PUT' ? updated : [old])) as typeof fetch;
  await mount();
  client.setQueryData(profileListKey(currentServerScope()), [old, { id: 'other' }]);
  client.setQueryData(profileDetailKey('p', currentServerScope()), old);
  let finish!: (value: typeof old) => void;
  const pending = client
    .fetchQuery({
      queryKey: profileDetailKey('p', currentServerScope()),
      staleTime: 0,
      queryFn: () =>
        new Promise<typeof old>((resolve) => {
          finish = resolve;
        }),
    })
    .catch(() => undefined);
  await act(async () => {
    await hooks.effects.mutateAsync({
      profileId: 'p',
      effectsChain: updated.effects_chain as never,
    });
  });
  finish(old);
  await pending;
  expect(client.getQueryData(profileDetailKey('p', currentServerScope()))).toEqual(updated);
  expect(client.getQueryData(profileListKey(currentServerScope()))).toEqual([updated, { id: 'other' }]);
});

test('deletion invalidates dependent caches only after success', async () => {
  let fail = true;
  globalThis.fetch = (async (_url, init) =>
    init?.method === 'DELETE' && fail
      ? Response.json({ detail: 'denied' }, { status: 403 })
      : Response.json([])) as typeof fetch;
  await mount();
  client.setQueryData(['history'], ['old']);
  client.setQueryData(['stories', 'detail'], { id: 'detail' });
  await act(async () => {
    await hooks.deleteProfile.mutateAsync('p').catch(() => undefined);
  });
  expect(client.getQueryState(['history'])?.isInvalidated).toBe(false);
  expect(client.getQueryState(['stories', 'detail'])?.isInvalidated).toBe(false);
  fail = false;
  await act(async () => {
    await hooks.deleteProfile.mutateAsync('p');
  });
  expect(client.getQueryState(['history'])?.isInvalidated).toBe(true);
  expect(client.getQueryState(['stories', 'detail'])?.isInvalidated).toBe(true);
});

test('story delete removes detail and preserves a changed selection', async () => {
  let finish!: () => void;
  globalThis.fetch = (async (_url, init) =>
    init?.method === 'DELETE'
      ? new Promise<Response>((resolve) => {
          finish = () => resolve(Response.json({}));
        })
      : Response.json([])) as typeof fetch;
  await mount();
  client.setQueryData(['stories'], [{ id: 'a' }, { id: 'b' }]);
  client.setQueryData(['stories', 'a'], { id: 'a' });
  useStoryStore.getState().setSelectedStoryId('a');
  let pending!: Promise<void>;
  await act(async () => {
    pending = hooks.deleteStory.mutateAsync('a');
  });
  useStoryStore.getState().setSelectedStoryId('b');
  await act(async () => {
    finish();
    await pending;
  });
  expect(client.getQueryData(['stories', 'a'])).toBeUndefined();
  expect(useStoryStore.getState().selectedStoryId).toBe('b');
});

test('story item mutation sends its payload and invalidates inactive detail', async () => {
  let body: unknown;
  globalThis.fetch = (async (_url, init) => {
    if (init?.method === 'PUT') body = JSON.parse(String(init.body));
    return Response.json([]);
  }) as typeof fetch;
  await mount();
  client.setQueryData(['stories', 's'], { id: 's' });
  await act(async () => {
    await hooks.move.mutateAsync({ storyId: 's', itemId: 'i', data: { start_time: 3 } as never });
  });
  expect(body).toEqual({ start_time: 3 });
  expect(client.getQueryState(['stories', 's'])?.isInvalidated).toBe(true);
});
