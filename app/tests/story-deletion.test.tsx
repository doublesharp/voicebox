import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { notifyManager, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import type { StoryResponse } from '../src/lib/api/types';
import { useStoryStore } from '../src/stores/storyStore';

let resolveDelete: () => void;
let rejectDelete: (error: Error) => void;
mock.module('../src/lib/api/client', () => ({
  apiClient: {
    deleteStory: () =>
      new Promise<void>((resolve, reject) => {
        resolveDelete = resolve;
        rejectDelete = reject;
      }),
  },
}));
const { useDeleteStory } = await import('../src/lib/hooks/useStories');
notifyManager.setScheduler((callback) => queueMicrotask(callback));
afterAll(() => notifyManager.setScheduler((callback) => setTimeout(callback, 0)));
let client: QueryClient;
let renderer: ReactTestRenderer;
let mutation: ReturnType<typeof useDeleteStory>;
const stories = [
  { id: 'a', name: 'First' },
  { id: 'b', name: 'Second' },
] as StoryResponse[];
function Probe() {
  mutation = useDeleteStory();
  return null;
}
async function mount(list = stories) {
  client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  client.setQueryData(['stories'], list);
  client.setQueryData(['stories', 'a'], { ...stories[0], items: [] });
  useStoryStore.getState().setSelectedStoryId('a');
  await act(async () => {
    renderer = create(
      <QueryClientProvider client={client}>
        <Probe />
      </QueryClientProvider>,
    );
  });
}
afterEach(async () => {
  await act(async () => renderer?.unmount());
  client?.clear();
  useStoryStore.getState().setSelectedStoryId(null);
});

test('deletion removes stale list and detail before the success callback can reselect them', async () => {
  await mount();
  let observed: unknown;
  await act(async () => {
    mutation.mutate('a', {
      onSuccess: () => {
        observed = {
          list: client.getQueryData(['stories']),
          detail: client.getQueryData(['stories', 'a']),
          selected: useStoryStore.getState().selectedStoryId,
        };
      },
    });
  });
  await act(async () => {
    resolveDelete();
  });
  expect(observed).toEqual({ list: [stories[1]], detail: undefined, selected: null });
});

test('deleting the only Story leaves no stale selection', async () => {
  await mount([stories[0]]);
  await act(async () => {
    mutation.mutate('a');
  });
  await act(async () => {
    resolveDelete();
  });
  expect(client.getQueryData(['stories'])).toEqual([]);
  expect(useStoryStore.getState().selectedStoryId).toBeNull();
});

test('selection changed while deletion is pending survives its acknowledgement', async () => {
  await mount();
  await act(async () => {
    mutation.mutate('a');
  });
  useStoryStore.getState().setSelectedStoryId('b');
  await act(async () => {
    resolveDelete();
  });
  expect(client.getQueryData(['stories'])).toEqual([stories[1]]);
  expect(useStoryStore.getState().selectedStoryId).toBe('b');
});

test('failed deletion preserves the list, detail and selection', async () => {
  await mount();
  await act(async () => {
    mutation.mutate('a');
  });
  await act(async () => {
    rejectDelete(new Error('Server unavailable'));
  });
  expect(client.getQueryData(['stories'])).toEqual(stories);
  expect(client.getQueryData(['stories', 'a'])).toMatchObject({ id: 'a' });
  expect(useStoryStore.getState().selectedStoryId).toBe('a');
});

test('an old in-flight list cannot restore the deleted Story after acknowledgement', async () => {
  await mount();
  let finishList!: (value: StoryResponse[]) => void;
  const pendingList = client
    .fetchQuery({
      queryKey: ['stories'],
      queryFn: () =>
        new Promise<StoryResponse[]>((resolve) => {
          finishList = resolve;
        }),
    })
    .catch(() => undefined);
  await act(async () => {
    mutation.mutate('a');
  });
  await act(async () => {
    resolveDelete();
  });
  finishList(stories);
  await pendingList;
  expect(client.getQueryData(['stories'])).toEqual([stories[1]]);
  expect(useStoryStore.getState().selectedStoryId).toBeNull();
});
