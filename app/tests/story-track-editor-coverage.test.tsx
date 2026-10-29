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

const listeners = new Map<string, Set<(event: any) => void>>();
const windowMock = {
  addEventListener: (name: string, fn: (event: any) => void) => {
    if (!listeners.has(name)) listeners.set(name, new Set());
    listeners.get(name)!.add(fn);
  },
  removeEventListener: (name: string, fn: (event: any) => void) => listeners.get(name)?.delete(fn),
};
(globalThis as any).window = windowMock;
(globalThis as any).HTMLInputElement = class {};
(globalThis as any).HTMLTextAreaElement = class {};
(globalThis as any).ResizeObserver = class {
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
function button(label: string) {
  return renderer.root.findAll((n) => n.type === 'button' && n.props['aria-label'] === label)[0];
}
function click(label: string) {
  act(() => button(label).props.onClick({ stopPropagation() {}, clientX: 0 }));
}
function select() {
  act(() =>
    renderer.root
      .findAllByType('button')
      .find((n) => n.findAllByType('p').some((p) => p.children.includes('Narrator')))!
      .props.onClick({ stopPropagation() {} }),
  );
}
function emit(name: string, event: any) {
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

test('empty editor is hidden and populated editor activates with trimmed duration', async () => {
  const activate = mock(() => {});
  useStoryStore.setState({ setActiveStory: activate });
  await mount([]);
  expect(renderer.toJSON()).toBeNull();
  await act(async () => renderer.update(<StoryTrackEditor storyId="story" items={[item]} />));
  expect(activate).toHaveBeenCalledWith('story', [item], 6000);
});

test('seek accounts for label and scrolling, clamps to zero, and clears selection', async () => {
  const seek = mock(() => {});
  useStoryStore.setState({ seek, playbackStoryId: 'story' });
  await mount();
  select();
  expect(useStoryStore.getState().selectedClipId).toBe('clip');
  node.scrollLeft = 20;
  act(() => button('Seek timeline').props.onClick({ clientX: 144 }));
  expect(seek).toHaveBeenLastCalledWith(2000);
  expect(useStoryStore.getState().selectedClipId).toBeNull();
  act(() => button('Seek timeline').props.onClick({ clientX: 0 }));
  expect(seek).toHaveBeenLastCalledWith(0);
});

test('split validates trimmed boundaries, rounds time, and clears selection only on success', async () => {
  useStoryStore.setState({ playbackStoryId: 'story', currentTimeMs: 2000 });
  await mount();
  select();
  click('Split at playhead');
  expect(mutations.split.mutate).not.toHaveBeenCalled();
  expect(toast).toHaveBeenLastCalledWith(expect.objectContaining({ title: 'Invalid split point' }));
  act(() => useStoryStore.setState({ currentTimeMs: 3500.6 }));
  click('Split at playhead');
  const [args, callbacks] = mutations.split.mutate.mock.calls[0] as any;
  expect(args).toEqual({ storyId: 'story', itemId: 'clip', data: { split_time_ms: 1501 } });
  act(() => callbacks.onError(new Error('offline')));
  expect(toast).toHaveBeenLastCalledWith(
    expect.objectContaining({ title: 'Failed to split clip', description: 'offline' }),
  );
  expect(useStoryStore.getState().selectedClipId).toBe('clip');
  act(() => callbacks.onSuccess());
  expect(useStoryStore.getState().selectedClipId).toBeNull();
});

test('duplicate and delete report failures and delete clears selection only after success', async () => {
  useStoryStore.setState({ playbackStoryId: 'story' });
  await mount();
  select();
  click('Duplicate clip');
  click('Delete clip');
  for (const [name, title] of [
    ['duplicate', 'Failed to duplicate clip'],
    ['remove', 'Failed to delete clip'],
  ] as const) {
    const [args, callbacks] = mutations[name].mutate.mock.calls[0] as any;
    expect(args).toEqual({ storyId: 'story', itemId: 'clip' });
    act(() => callbacks.onError(new Error('denied')));
    expect(toast).toHaveBeenLastCalledWith(
      expect.objectContaining({ title, description: 'denied' }),
    );
  }
  expect(useStoryStore.getState().selectedClipId).toBe('clip');
  act(() => (mutations.remove.mutate.mock.calls[0] as any)[1].onSuccess());
  expect(useStoryStore.getState().selectedClipId).toBeNull();
});

test('keyboard shortcuts ignore text inputs and act on the selection', async () => {
  useStoryStore.setState({ playbackStoryId: 'story' });
  await mount();
  select();
  const preventDefault = mock(() => {});
  emit('keydown', {
    key: 'Delete',
    target: new (globalThis as any).HTMLInputElement(),
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

test('track controls extend above and below existing rows', async () => {
  useStoryStore.setState({ playbackStoryId: 'story' });
  await mount();
  click('Add track above');
  click('Add track below');
  const labels = renderer.root
    .findAll((n) => n.type === 'span' && n.props.className?.includes('select-none'))
    .map((n) => n.children.join(''));
  expect(labels.filter((label) => /^-?\d+$/.test(label))).toEqual(['2', '1', '0', '-1', '-2']);
});

test('trim commits changed integer offsets and reports mutation failures', async () => {
  useStoryStore.setState({ playbackStoryId: 'story' });
  await mount();
  select();
  act(() => button('Trim start').props.onMouseDown({ stopPropagation() {}, clientX: 100 }));
  emit('mousemove', { clientX: 125 });
  emit('mouseup', {});
  const [args, callbacks] = mutations.trim.mutate.mock.calls[0] as any;
  expect(args).toEqual({
    storyId: 'story',
    itemId: 'clip',
    data: { trim_start_ms: 1000, trim_end_ms: 500 },
  });
  act(() => callbacks.onError(new Error('offline')));
  expect(toast).toHaveBeenLastCalledWith(
    expect.objectContaining({ title: 'Failed to trim clip', description: 'offline' }),
  );
});

test('drag moves a clip to the measured time and track, with failure feedback', async () => {
  useStoryStore.setState({ playbackStoryId: 'story' });
  await mount();
  const clip = renderer.root
    .findAllByType('button')
    .find((n) => n.findAllByType('p').some((p) => p.children.includes('Narrator')))!;
  act(() =>
    clip.props.onMouseDown({
      stopPropagation() {},
      clientX: 164,
      clientY: 72,
      target: { closest: () => null },
      currentTarget: { getBoundingClientRect: () => ({ left: 164, top: 72 }) },
    }),
  );
  const container = renderer.root.findAll(
    (n) => n.type === 'div' && n.props.className === 'overflow-auto relative',
  )[0];
  act(() => container.props.onMouseMove({ clientX: 214, clientY: 120 }));
  act(() => container.props.onMouseUp());
  const [args, callbacks] = mutations.move.mutate.mock.calls[0] as any;
  expect(args).toEqual({
    storyId: 'story',
    itemId: 'clip',
    data: { start_time_ms: 3000, track: -1 },
  });
  act(() => callbacks.onError(new Error('offline')));
  expect(toast).toHaveBeenLastCalledWith(
    expect.objectContaining({ title: 'Failed to move item', description: 'offline' }),
  );
});

test('play starts clips in timeline order and pause and stop use playback state', async () => {
  const play = mock(() => {});
  const pause = mock(() => {});
  const stop = mock(() => {});
  useStoryStore.setState({ playbackStoryId: 'story', play, pause, stop });
  const earlier = { ...item, id: 'earlier', start_time_ms: 0 };
  await mount([item, earlier]);
  click('Play');
  expect(play).toHaveBeenCalledWith('story', [earlier, item]);
  act(() => useStoryStore.setState({ isPlaying: true }));
  click('Pause');
  click('Stop');
  expect(pause).toHaveBeenCalledTimes(1);
  expect(stop).toHaveBeenCalledTimes(1);
});
