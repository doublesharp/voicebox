import { afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import type { StoryItemDetail } from '../src/lib/api/types';
import { useStoryStore } from '../src/stores/storyStore';

const mutations = Object.fromEntries(
  ['move', 'trim', 'split', 'duplicate', 'remove', 'version', 'volume'].map((name) => [
    name,
    { mutate: mock(() => {}), isPending: false },
  ]),
);
const toast = mock(() => {});
const regenerate = mock(async () => {});
mock.module('../src/lib/hooks/useStories', () => ({
  useMoveStoryItem: () => mutations.move,
  useTrimStoryItem: () => mutations.trim,
  useSplitStoryItem: () => mutations.split,
  useDuplicateStoryItem: () => mutations.duplicate,
  useRemoveStoryItem: () => mutations.remove,
  useSetStoryItemVersion: () => mutations.version,
  useUpdateStoryItemVolume: () => mutations.volume,
}));
mock.module('../src/components/ui/use-toast', () => ({ useToast: () => ({ toast }) }));
mock.module('../src/lib/api/client', () => ({ apiClient: { regenerateGeneration: regenerate } }));
mock.module('../src/lib/audio/storyWaveform', () => ({ StoryClipWaveform: () => null }));
mock.module('../src/components/StoriesTab/CreateStudioCopyButton', () => ({
  CreateStudioCopyButton: () => null,
}));
const { StoryTrackEditor } = await import('../src/components/StoriesTab/StoryTrackEditor');

type KeyEvent = Omit<Partial<KeyboardEvent>, 'target'> & { target?: unknown };
const globals = globalThis as unknown as Record<string, unknown>;
const listeners = new Map<string, Set<(event: KeyEvent) => void>>();
const windowMock = {
  addEventListener: (name: string, fn: (event: KeyEvent) => void) => {
    if (!listeners.has(name)) listeners.set(name, new Set());
    listeners.get(name)!.add(fn);
  },
  removeEventListener: (name: string, fn: (event: KeyEvent) => void) =>
    listeners.get(name)?.delete(fn),
};
globals.window = windowMock;
globals.HTMLInputElement = class {};
globals.HTMLTextAreaElement = class {};
globals.ResizeObserver = class {
  observe() {}
  disconnect() {}
};
const item = {
  id: 'clip',
  generation_id: 'gen',
  profile_name: 'Narrator',
  engine: 'test',
  duration: 5,
  start_time_ms: 2000,
  track: 0,
  trim_start_ms: 500,
  trim_end_ms: 500,
  volume: 1,
} as StoryItemDetail;
let renderer: ReactTestRenderer;
const node = {
  clientWidth: 0,
  scrollLeft: 0,
  addEventListener() {},
  removeEventListener() {},
  getBoundingClientRect: () => ({ left: 0, top: 0 }),
};
async function mount(items = [item]) {
  await act(async () => {
    renderer = create(<StoryTrackEditor storyId="story" items={items} />, {
      createNodeMock: () => node,
    });
  });
}
function select() {
  act(() =>
    renderer.root
      .findAllByType('button')
      .find((n) => n.findAllByType('p').some((p) => p.children.includes('Narrator')))!
      .props.onClick({ stopPropagation() {} }),
  );
}
function emit(name: string, event: KeyEvent) {
  act(() => {
    for (const fn of [...(listeners.get(name) ?? [])]) fn(event);
  });
}
afterEach(() => {
  act(() => renderer?.unmount());
  useStoryStore.setState({
    selectedClipId: null,
    playbackStoryId: null,
    currentTimeMs: 0,
    isPlaying: false,
    playbackError: null,
  });
  for (const mutation of Object.values(mutations)) {
    mutation.mutate.mockClear();
    mutation.isPending = false;
  }
  toast.mockClear();
  regenerate.mockClear();
  listeners.clear();
  node.scrollLeft = 0;
});

test('keyboard shortcuts ignore text inputs and act on the selection', async () => {
  useStoryStore.setState({ playbackStoryId: 'story' });
  await mount();
  select();
  const preventDefault = mock(() => {});
  emit('keydown', {
    key: 'Delete',
    target: new (globals.HTMLInputElement as new () => HTMLInputElement)(),
    preventDefault,
  });
  expect(mutations.remove.mutate).not.toHaveBeenCalled();
  for (const event of [
    { key: 'd', ctrlKey: true, target: { closest: () => ({}) } },
    { key: 'Delete', repeat: true, target: {} },
    { key: 's', isComposing: true, target: {} },
    { key: 's', ctrlKey: true, target: {} },
  ])
    emit('keydown', { ...event, preventDefault });
  expect(mutations.duplicate.mutate).not.toHaveBeenCalled();
  expect(mutations.remove.mutate).not.toHaveBeenCalled();
  expect(mutations.split.mutate).not.toHaveBeenCalled();
  emit('keydown', { key: 'd', ctrlKey: true, target: {}, preventDefault });
  expect(mutations.duplicate.mutate).toHaveBeenCalledTimes(1);
  emit('keydown', { key: 'Escape', target: {}, preventDefault });
  expect(useStoryStore.getState().selectedClipId).toBeNull();
  emit('keydown', { key: 'Delete', target: {}, preventDefault });
  expect(mutations.remove.mutate).not.toHaveBeenCalled();
});
