import { afterAll, afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import type { StoryItemDetail } from '../src/lib/api/types';
import { useStoryPlayback } from '../src/lib/hooks/useStoryPlayback';
import { useStoryStore } from '../src/stores/storyStore';

class FakeSource {
  buffer: unknown;
  onended: (() => void) | null = null;
  starts: number[][] = [];
  stops = 0;
  disconnected = false;
  connect() {}
  disconnect() {
    this.disconnected = true;
  }
  start(...args: number[]) {
    this.starts.push(args);
  }
  stop() {
    this.stops++;
  }
}

class FakeContext {
  static instances: FakeContext[] = [];
  currentTime = 10;
  state = 'running';
  sampleRate = 24000;
  destination = {};
  sources: FakeSource[] = [];
  constructor() {
    FakeContext.instances.push(this);
  }
  createGain() {
    return { gain: { value: 1 }, connect() {}, disconnect() {} };
  }
  createBufferSource() {
    const source = new FakeSource();
    this.sources.push(source);
    return source;
  }
  async decodeAudioData() {
    return {
      duration: 1,
      length: 24000,
      numberOfChannels: 1,
      sampleRate: 24000,
      getChannelData: () => new Float32Array(24000).fill(0.1),
    };
  }
  async resume() {
    this.state = 'running';
  }
  async close() {
    this.state = 'closed';
  }
}

const originals = {
  fetch: globalThis.fetch,
  context: globalThis.AudioContext,
  raf: globalThis.requestAnimationFrame,
  cancelRaf: globalThis.cancelAnimationFrame,
};
let renderer: ReactTestRenderer | undefined;
let rafCallbacks: Map<number, FrameRequestCallback>;
let rafSequence = 0;
const item = (id: string, source = id, start = 0) =>
  ({
    id,
    story_id: 'story',
    generation_id: source,
    start_time_ms: start,
    track: 0,
    trim_start_ms: 0,
    trim_end_ms: 0,
    duration: 1,
    volume: 1,
    audio_path: `${source}.wav`,
  }) as StoryItemDetail;
function Harness({ items }: { items: StoryItemDetail[] }) {
  useStoryPlayback(items);
  return null;
}
const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  FakeContext.instances = [];
  rafCallbacks = new Map();
  globalThis.AudioContext = FakeContext as unknown as typeof AudioContext;
  globalThis.requestAnimationFrame = (callback) => {
    const id = ++rafSequence;
    rafCallbacks.set(id, callback);
    return id;
  };
  globalThis.cancelAnimationFrame = (id) => {
    rafCallbacks.delete(id);
  };
  globalThis.fetch = mock(async () => ({
    ok: true,
    arrayBuffer: async () => new ArrayBuffer(4),
  })) as unknown as typeof fetch;
  useStoryStore.setState({
    isPlaying: false,
    playbackItems: null,
    playbackStoryId: null,
    playbackStartContextTime: null,
    playbackStartStoryTime: null,
    currentTimeMs: 0,
    totalDurationMs: 0,
  });
});
afterEach(async () => {
  if (renderer) {
    await act(async () => renderer?.unmount());
    renderer = undefined;
  }
  useStoryStore.getState().stop();
});
afterAll(() => {
  globalThis.fetch = originals.fetch;
  globalThis.AudioContext = originals.context;
  globalThis.requestAnimationFrame = originals.raf;
  globalThis.cancelAnimationFrame = originals.cancelRaf;
});

test('split clips and rerenders share a pending fetch/decode', async () => {
  let finish!: (value: unknown) => void;
  const pending = new Promise((resolve) => {
    finish = resolve;
  });
  const fetcher = mock(() => pending);
  globalThis.fetch = fetcher as unknown as typeof fetch;
  const items = [item('split-a', 'shared-pending'), item('split-b', 'shared-pending', 1000)];
  await act(async () => {
    renderer = create(<Harness items={items} />);
  });
  await act(async () => {
    renderer?.update(<Harness items={[...items]} />);
  });
  expect(fetcher).toHaveBeenCalledTimes(1);
  finish({ ok: true, arrayBuffer: async () => new ArrayBuffer(4) });
  await act(flush);
});

test('future clips are scheduled on the audio clock without animation frames', async () => {
  const items = [item('ahead-a'), item('ahead-b', 'ahead-b', 1000)];
  await act(async () => {
    renderer = create(<Harness items={items} />);
    await flush();
  });
  await act(async () => {
    useStoryStore.getState().play('story', items);
    await flush();
  });
  const context = FakeContext.instances.find((candidate) => candidate.sources.length > 0)!;
  expect(context.sources).toHaveLength(2);
  expect(context.sources[1].starts[0][0]).toBeGreaterThanOrEqual(11);
  expect(context.sources[1].starts[0][1]).toBe(0);
  expect(rafCallbacks.size).toBeGreaterThan(0);
});

test('starting before audio is ready preserves the first samples', async () => {
  let finish!: (value: unknown) => void;
  globalThis.fetch = mock(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  ) as unknown as typeof fetch;
  const items = [item('late-buffer')];
  await act(async () => {
    renderer = create(<Harness items={items} />);
  });
  await act(async () => {
    useStoryStore.getState().play('story', items);
  });
  expect(useStoryStore.getState().playbackStartContextTime).toBeNull();
  for (const context of FakeContext.instances) context.currentTime = 20;
  finish({ ok: true, arrayBuffer: async () => new ArrayBuffer(4) });
  await act(flush);
  const context = FakeContext.instances.find((candidate) => candidate.sources.length > 0)!;
  expect(context.sources[0].starts[0][0]).toBeGreaterThanOrEqual(20);
  expect(context.sources[0].starts[0][1]).toBe(0);
});

test('unmount aborts stale audio requests', async () => {
  let aborted = false;
  globalThis.fetch = mock(
    (_url, options) =>
      new Promise((_resolve, reject) => {
        options?.signal?.addEventListener('abort', () => {
          aborted = true;
          reject(new DOMException('Cancelled', 'AbortError'));
        });
      }),
  ) as typeof fetch;
  await act(async () => {
    renderer = create(<Harness items={[item('stale-request')]} />);
  });
  await act(async () => {
    renderer?.unmount();
    renderer = undefined;
  });
  expect(aborted).toBe(true);
});

test('pause disconnects every scheduled source and cancels playhead frames', async () => {
  const items = [item('pause-a'), item('pause-b', 'pause-b', 1000)];
  await act(async () => {
    renderer = create(<Harness items={items} />);
    await flush();
  });
  await act(async () => {
    useStoryStore.getState().play('story', items);
    await flush();
  });
  const sources = FakeContext.instances.flatMap((context) => context.sources);
  expect(sources.length).toBeGreaterThan(0);
  await act(async () => {
    useStoryStore.getState().pause();
  });
  expect(sources.every((source) => source.stops > 0 && source.disconnected)).toBe(true);
  expect(rafCallbacks.size).toBe(0);
});

test('naturally ended clips never restart on a scheduler tick', async () => {
  const items = [item('ended-a'), item('ended-b', 'ended-b', 1000)];
  await act(async () => {
    renderer = create(<Harness items={items} />);
    await flush();
  });
  await act(async () => {
    useStoryStore.getState().play('story', items);
    await flush();
  });
  const context = FakeContext.instances.find((candidate) => candidate.sources.length > 0)!;
  context.sources[0].onended?.();
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 70));
  });
  expect(context.sources).toHaveLength(2);
});

test('seek keeps trim offsets and stale ended callbacks cannot remove replacement sources', async () => {
  const items = [{ ...item('seek-source'), trim_start_ms: 100, trim_end_ms: 100 }];
  await act(async () => {
    renderer = create(<Harness items={items} />);
    await flush();
  });
  await act(async () => {
    useStoryStore.getState().play('story', items);
    await flush();
  });
  const context = FakeContext.instances.find((candidate) => candidate.sources.length > 0)!;
  const oldEnded = context.sources[0].onended;
  await act(async () => {
    useStoryStore.getState().seek(200);
    await flush();
  });
  const replacement = context.sources[1];
  expect(replacement.starts[0][1]).toBeCloseTo(0.3);
  expect(replacement.starts[0][2]).toBeCloseTo(0.6);
  oldEnded?.();
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 70));
  });
  expect(context.sources).toHaveLength(2);
  expect(replacement.stops).toBe(0);
});

test('changing the audio revision while playing replaces the stale source', async () => {
  const items = [item('revision-source')];
  await act(async () => {
    renderer = create(<Harness items={items} />);
    await flush();
  });
  await act(async () => {
    useStoryStore.getState().play('story', items);
    await flush();
  });
  const context = FakeContext.instances.find((candidate) => candidate.sources.length > 0)!;
  await act(async () => {
    renderer?.update(<Harness items={[{ ...items[0], audio_path: 'revision-updated.wav' }]} />);
    await flush();
  });
  expect(globalThis.fetch).toHaveBeenCalledTimes(2);
  expect(context.sources[0].stops).toBe(1);
  expect(context.sources).toHaveLength(2);
});

test('switching to another story keeps its own playhead position', async () => {
  const items = [item('switch-source')];
  await act(async () => {
    renderer = create(<Harness items={items} />);
    await flush();
  });
  await act(async () => {
    useStoryStore.getState().play('story', items);
    await flush();
  });
  const context = FakeContext.instances.find((candidate) => candidate.sources.length > 0)!;
  context.currentTime += 0.5;
  await act(async () => {
    useStoryStore.getState().setActiveStory('other-story', [item('other-source')], 1000);
  });
  expect(useStoryStore.getState().currentTimeMs).toBe(0);
  expect(context.sources[0].stops).toBe(1);
});

test('a long scheduler stall re-buffers an entirely missed clip instead of dropping it', async () => {
  const items = [item('stall-first'), item('stall-next', 'stall-next', 40_000)];
  await act(async () => {
    renderer = create(<Harness items={items} />);
    await flush();
  });
  await act(async () => {
    useStoryStore.getState().play('story', items);
    await flush();
  });
  const context = FakeContext.instances.find((candidate) => candidate.sources.length > 0)!;
  expect(context.sources).toHaveLength(1);
  context.currentTime = 100;
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 70));
  });
  expect(context.sources).toHaveLength(2);
  expect(context.sources[1].starts[0][1]).toBe(0);
  expect(useStoryStore.getState().currentTimeMs).toBe(40_000);
});

test('a preloaded but unscheduled clip after a stall starts from its boundary', async () => {
  const { storyAudioCache, storyAudioSource } = await import('../src/lib/audio/storyAudioCache');
  const items = [item('ready-stall-first'), item('ready-stall-next', 'ready-stall-next', 40_000)];
  await act(async () => {
    renderer = create(<Harness items={items} />);
    await flush();
  });
  await act(async () => {
    useStoryStore.getState().play('story', items);
    await flush();
  });
  const context = FakeContext.instances.find((candidate) => candidate.sources.length > 0)!;
  const ready = storyAudioCache.retain(storyAudioSource(items[1]));
  await ready.promise;
  context.currentTime = 50.5;
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 70));
  });
  expect(context.sources).toHaveLength(2);
  expect(context.sources[1].starts[0][1]).toBe(0);
  expect(useStoryStore.getState().currentTimeMs).toBe(40_000);
  ready.release();
});

test('removing the final clip from the selected playing story stops its audio', async () => {
  useStoryStore.getState().setSelectedStoryId('story');
  const items = [item('removed-source')];
  await act(async () => {
    renderer = create(<Harness items={items} />);
    await flush();
  });
  await act(async () => {
    useStoryStore.getState().play('story', items);
    await flush();
  });
  const context = FakeContext.instances.find((candidate) => candidate.sources.length > 0)!;
  await act(async () => {
    renderer?.update(<Harness items={[]} />);
  });
  expect(useStoryStore.getState().isPlaying).toBe(false);
  expect(context.sources[0].stops).toBe(1);
});
