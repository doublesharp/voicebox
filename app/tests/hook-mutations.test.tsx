import { afterAll, afterEach, expect, test } from 'bun:test';
import { notifyManager, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import * as P from '../src/lib/hooks/useProfiles';
import * as S from '../src/lib/hooks/useStories';
import { PlatformProvider } from '../src/platform/PlatformContext';
import type { Platform } from '../src/platform/types';

notifyManager.setScheduler((callback) => queueMicrotask(callback));
afterAll(() => notifyManager.setScheduler((callback) => setTimeout(callback, 0)));
const originalFetch = globalThis.fetch;
let view: ReactTestRenderer;
let client: QueryClient;
let hooks: ReturnType<typeof useAll>;
const saves: Array<[string, Blob]> = [];
const platform = {
  filesystem: {
    saveFile: async (name: string, blob: Blob) => {
      saves.push([name, blob]);
    },
  },
} as unknown as Platform;
function useAll() {
  return {
    createProfile: P.useCreateProfile(),
    updateProfile: P.useUpdateProfile(),
    addSample: P.useAddSample(),
    deleteSample: P.useDeleteSample(),
    updateSample: P.useUpdateSample(),
    importProfile: P.useImportProfile(),
    uploadAvatar: P.useUploadAvatar(),
    deleteAvatar: P.useDeleteAvatar(),
    exportSample: P.useExportSampleAudio(),
    exportProfile: P.useExportProfile(),
    createStory: S.useCreateStory(),
    updateStory: S.useUpdateStory(),
    add: S.useAddStoryItem(),
    remove: S.useRemoveStoryItem(),
    times: S.useUpdateStoryItemTimes(),
    reorder: S.useReorderStoryItems(),
    move: S.useMoveStoryItem(),
    trim: S.useTrimStoryItem(),
    volume: S.useUpdateStoryItemVolume(),
    split: S.useSplitStoryItem(),
    duplicate: S.useDuplicateStoryItem(),
    version: S.useSetStoryItemVersion(),
    exportStory: S.useExportStoryAudio(),
  };
}
function Probe() {
  hooks = useAll();
  return null;
}
async function mount() {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } },
  });
  await act(async () => {
    view = create(
      <PlatformProvider platform={platform}>
        <QueryClientProvider client={client}>
          <Probe />
        </QueryClientProvider>
      </PlatformProvider>,
    );
  });
}
afterEach(() => {
  act(() => view?.unmount());
  client?.clear();
  globalThis.fetch = originalFetch;
  saves.length = 0;
});

test('profile and story mutations invalidate their dependent caches only on success', async () => {
  let failure = false;
  let captured = '';
  globalThis.fetch = (async (url) => {
    captured = new URL(String(url)).pathname;
    return failure
      ? Response.json({ detail: 'denied' }, { status: 403 })
      : Response.json({ id: 'p', profile_id: 'p' });
  }) as typeof fetch;
  await mount();
  const file = new File(['audio'], 'a.wav');
  const data = { name: 'new' } as never;
  const cases: Array<[string, () => Promise<unknown>, string]> = [
    ['profiles', () => hooks.createProfile.mutateAsync(data), '/profiles'],
    ['profiles', () => hooks.updateProfile.mutateAsync({ profileId: 'p', data }), '/profiles/p'],
    [
      'profiles',
      () => hooks.addSample.mutateAsync({ profileId: 'p', file, referenceText: 'words' }),
      '/profiles/p/samples',
    ],
    ['profiles', () => hooks.deleteSample.mutateAsync('a'), '/profiles/samples/a'],
    [
      'profiles',
      () => hooks.updateSample.mutateAsync({ sampleId: 'a', referenceText: 'words' }),
      '/profiles/samples/a',
    ],
    ['profiles', () => hooks.importProfile.mutateAsync(file), '/profiles/import'],
    [
      'profiles',
      () => hooks.uploadAvatar.mutateAsync({ profileId: 'p', file }),
      '/profiles/p/avatar',
    ],
    ['profiles', () => hooks.deleteAvatar.mutateAsync('p'), '/profiles/p/avatar'],
    ['stories', () => hooks.createStory.mutateAsync(data), '/stories'],
    ['stories', () => hooks.updateStory.mutateAsync({ storyId: 's', data }), '/stories/s'],
    ['stories', () => hooks.add.mutateAsync({ storyId: 's', data }), '/stories/s/items'],
    [
      'stories',
      () => hooks.remove.mutateAsync({ storyId: 's', itemId: 'i' }),
      '/stories/s/items/i',
    ],
    ['stories', () => hooks.times.mutateAsync({ storyId: 's', data }), '/stories/s/items/times'],
    [
      'stories',
      () => hooks.reorder.mutateAsync({ storyId: 's', data }),
      '/stories/s/items/reorder',
    ],
    [
      'stories',
      () => hooks.move.mutateAsync({ storyId: 's', itemId: 'i', data }),
      '/stories/s/items/i/move',
    ],
    [
      'stories',
      () => hooks.trim.mutateAsync({ storyId: 's', itemId: 'i', data }),
      '/stories/s/items/i/trim',
    ],
    [
      'stories',
      () => hooks.volume.mutateAsync({ storyId: 's', itemId: 'i', data }),
      '/stories/s/items/i/volume',
    ],
    [
      'stories',
      () => hooks.split.mutateAsync({ storyId: 's', itemId: 'i', data }),
      '/stories/s/items/i/split',
    ],
    [
      'stories',
      () => hooks.duplicate.mutateAsync({ storyId: 's', itemId: 'i' }),
      '/stories/s/items/i/duplicate',
    ],
    [
      'stories',
      () => hooks.version.mutateAsync({ storyId: 's', itemId: 'i', data }),
      '/stories/s/items/i/version',
    ],
  ];
  for (const [key, call, path] of cases) {
    client.setQueryData([key], []);
    client.setQueryData([key, key === 'profiles' ? 'p' : 's'], {});
    const target = path === '/profiles/p/samples' ? [key, 'p'] : [key];
    failure = true;
    await act(async () => {
      await expect(call()).rejects.toThrow('denied');
    });
    expect(client.getQueryState(target)?.isInvalidated).toBe(false);
    failure = false;
    await act(async () => {
      await call();
    });
    expect(captured).toBe(path);
    expect(client.getQueryState(target)?.isInvalidated).toBe(true);
  }
});

test('exports save sanitized filenames and propagate fetch and filesystem errors', async () => {
  globalThis.fetch = (async (url) =>
    String(url).endsWith('/profiles/p')
      ? Response.json({ name: 'My Voice!' })
      : new Response('audio')) as typeof fetch;
  await mount();
  await act(async () => {
    await hooks.exportSample.mutateAsync({
      sampleId: '123456789',
      referenceText: 'Hello, World!',
      format: 'mp3',
    });
    await hooks.exportProfile.mutateAsync('p');
    await hooks.exportStory.mutateAsync({ storyId: 's', storyName: 'A Story!', format: 'mp3' });
    await hooks.exportStory.mutateAsync({ storyId: 's', storyName: '' });
  });
  expect(saves.map(([name]) => name)).toEqual([
    'sample-hello--world--12345678.mp3',
    'profile-my-voice-.voicebox.zip',
    'a-story-.mp3',
    'story.wav',
  ]);
  expect(await saves[0][1].text()).toBe('audio');
  globalThis.fetch = (async () =>
    Response.json({ detail: 'export denied' }, { status: 403 })) as typeof fetch;
  await act(async () => {
    await expect(hooks.exportStory.mutateAsync({ storyId: 's', storyName: 'x' })).rejects.toThrow(
      'export denied',
    );
  });
  expect(saves).toHaveLength(4);
});

test('filesystem failure from export reaches the caller', async () => {
  globalThis.fetch = (async () => new Response('audio')) as typeof fetch;
  await mount();
  const originalSave = platform.filesystem.saveFile;
  platform.filesystem.saveFile = async () => {
    throw new Error('disk full');
  };
  try {
    await act(async () => {
      await expect(hooks.exportStory.mutateAsync({ storyId: 's', storyName: 'x' })).rejects.toThrow(
        'disk full',
      );
    });
  } finally {
    platform.filesystem.saveFile = originalSave;
  }
});
