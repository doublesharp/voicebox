import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { usePlayerStore } from '../src/stores/playerStore';

const actualQuery = await import('@tanstack/react-query');
const actualPlatform = await import('../src/platform/PlatformContext');
const actualWaveSurfer = await import('wavesurfer.js');
const actualSlider = await import('../src/components/ui/slider');
const originalDocument = globalThis.document;
const originalWindow = globalThis.window;
const originalRaf = globalThis.requestAnimationFrame;
const originalCancel = globalThis.cancelAnimationFrame;
const originalStyle = globalThis.getComputedStyle;
let keydown: ((event: KeyboardEvent) => void) | undefined;
const style = { display: 'block', visibility: 'visible', getPropertyValue: () => '0 0% 100%' };
globalThis.document = {
  documentElement: {},
  addEventListener: (name: string, listener: (event: KeyboardEvent) => void) => {
    if (name === 'keydown') keydown = listener;
  },
  removeEventListener: (name: string, listener: (event: KeyboardEvent) => void) => {
    if (name === 'keydown' && keydown === listener) keydown = undefined;
  },
} as unknown as Document;
globalThis.window = { getComputedStyle: () => style } as unknown as Window & typeof globalThis;
globalThis.getComputedStyle = () => style as unknown as CSSStyleDeclaration;
globalThis.requestAnimationFrame = (callback) => {
  queueMicrotask(() => callback(0));
  return 1;
};
globalThis.cancelAnimationFrame = () => {};

function makeWave() {
  const handlers = new Map<string, Set<(...args: unknown[]) => void>>();
  let finish!: () => void;
  let reject!: (error: Error) => void;
  let duration = 0;
  let position = 0;
  let playing = false;
  const context = { state: 'running', currentTime: 0, close: mock(async () => {}) };
  const wave = {
    url: '',
    context,
    on: (name: string, callback: (...args: unknown[]) => void) => {
      const listeners = handlers.get(name) ?? new Set();
      listeners.add(callback);
      handlers.set(name, listeners);
      return () => listeners.delete(callback);
    },
    emit: (name: string, ...args: unknown[]) =>
      handlers.get(name)?.forEach((fn) => {
        fn(...args);
      }),
    load: (url: string) => {
      wave.url = url;
      return new Promise<void>((resolve, failed) => {
        finish = resolve;
        reject = failed;
      });
    },
    ready: (seconds: number) => {
      duration = seconds;
      wave.emit('ready');
      finish();
    },
    fail: () => {
      const error = new Error('Old clip failed');
      wave.emit('error', error);
      reject(error);
    },
    getDuration: () => duration,
    getPlaybackRate: () => 1,
    getCurrentTime: () => position,
    getMediaElement: () => ({
      getGainNode: () => ({ context }),
      removeAttribute: mock(() => {}),
    }),
    setVolume: () => {},
    setMuted: () => {},
    seekTo: (progress: number) => {
      position = duration * progress;
    },
    setTime: (time: number) => {
      position = time;
    },
    isPlaying: () => playing,
    play: mock(async () => {
      playing = true;
      wave.emit('play');
    }),
    pause: () => {
      playing = false;
      wave.emit('pause');
    },
    destroy: mock(() => {}),
  };
  return wave;
}
const waves: ReturnType<typeof makeWave>[] = [];
mock.module('@tanstack/react-query', () => ({ ...actualQuery, useQuery: () => ({}) }));
mock.module('../src/platform/PlatformContext', () => ({
  ...actualPlatform,
  usePlatform: () => ({ metadata: { isTauri: false }, audio: {} }),
}));
mock.module('wavesurfer.js', () => ({
  ...actualWaveSurfer,
  default: {
    create: () => {
      const wave = makeWave();
      waves.push(wave);
      return wave;
    },
  },
}));
mock.module('../src/components/ui/slider', () => ({
  Slider: (props: Record<string, unknown>) => <div {...props} />,
}));
const { AudioPlayer } = await import('../src/components/AudioPlayer/AudioPlayer');
let renderer: ReactTestRenderer;
async function mount() {
  await act(async () => {
    renderer = create(<AudioPlayer />, {
      createNodeMock: () => ({ getBoundingClientRect: () => ({ width: 800, height: 80 }) }),
    });
  });
}
afterEach(async () => {
  await act(async () => renderer?.unmount());
  usePlayerStore.getState().reset();
  waves.length = 0;
});
afterAll(() => {
  globalThis.document = originalDocument;
  globalThis.window = originalWindow;
  globalThis.requestAnimationFrame = originalRaf;
  globalThis.cancelAnimationFrame = originalCancel;
  globalThis.getComputedStyle = originalStyle;
  mock.module('@tanstack/react-query', () => actualQuery);
  mock.module('../src/platform/PlatformContext', () => actualPlatform);
  mock.module('wavesurfer.js', () => actualWaveSurfer);
  mock.module('../src/components/ui/slider', () => actualSlider);
});

test('a delayed old decode cannot replace or play the newly selected clip', async () => {
  usePlayerStore.getState().setAudioWithAutoPlay('/audio/a', 'a', null);
  await mount();
  const old = waves[0];
  await act(async () => usePlayerStore.getState().setAudioWithAutoPlay('/audio/b', 'b', null));
  const current = waves.at(-1)!;
  expect(current).not.toBe(old);
  await act(async () => current.ready(7));
  expect(current.play).toHaveBeenCalledTimes(1);
  await act(async () => old.ready(3));
  expect(usePlayerStore.getState()).toMatchObject({ audioId: 'b', duration: 7, isPlaying: true });
  expect(old.play).not.toHaveBeenCalled();
  expect(old.destroy).toHaveBeenCalledTimes(1);
  expect(old.context.close).toHaveBeenCalledTimes(1);
});

test('an old load failure cannot stop autoplay of the new clip', async () => {
  usePlayerStore.getState().setAudioWithAutoPlay('/audio/a', 'a', null);
  await mount();
  const old = waves[0];
  await act(async () => usePlayerStore.getState().setAudioWithAutoPlay('/audio/b', 'b', null));
  const current = waves.at(-1)!;
  await act(async () => old.fail());
  await act(async () => current.ready(9));
  expect(usePlayerStore.getState()).toMatchObject({ audioId: 'b', duration: 9, isPlaying: true });
  expect(current.play).toHaveBeenCalledTimes(1);
  expect(renderer.root.findAllByProps({ role: 'alert' })).toHaveLength(0);
});

test('a batched A to B to A selection starts a fresh playable session', async () => {
  usePlayerStore.getState().setAudioWithAutoPlay('/audio/a', 'a', null);
  await mount();
  const original = waves[0];
  await act(async () => original.ready(3));
  expect(usePlayerStore.getState().duration).toBe(3);
  await act(async () => {
    usePlayerStore.getState().setAudioWithAutoPlay('/audio/b', 'b', null);
    usePlayerStore.getState().setAudioWithAutoPlay('/audio/a', 'a', null);
  });
  const current = waves.at(-1)!;
  expect(current).not.toBe(original);
  await act(async () => current.ready(3));
  await act(async () => original.ready(9));
  expect(usePlayerStore.getState()).toMatchObject({ audioId: 'a', duration: 3, isPlaying: true });
  expect(current.play).toHaveBeenCalledTimes(1);
});

test('changing the selected clip immediately clears its predecessor duration', () => {
  const player = usePlayerStore.getState();
  player.setAudio('/audio/a', 'a', null);
  player.setDuration(25);
  player.setAudioWithAutoPlay('/audio/b', 'b', null);
  expect(usePlayerStore.getState()).toMatchObject({ audioId: 'b', duration: 0 });
});

test('space on an interactive control is not intercepted by the player shortcut', async () => {
  usePlayerStore.getState().setAudio('/audio/a', 'a', null);
  await mount();
  await act(async () => waves[0].ready(3));
  for (const tagName of ['BUTTON', 'DIV']) {
    const preventDefault = mock(() => {});
    const stopPropagation = mock(() => {});
    const target = {
      tagName,
      isContentEditable: false,
      closest: (selector: string) =>
        tagName === 'BUTTON'
          ? selector.includes('button')
            ? {}
            : null
          : selector.includes('[role="button"]')
            ? {}
            : null,
    };
    await act(async () =>
      keydown!({
        code: 'Space',
        target,
        preventDefault,
        stopPropagation,
      } as unknown as KeyboardEvent),
    );
    expect(preventDefault).not.toHaveBeenCalled();
    expect(stopPropagation).not.toHaveBeenCalled();
    expect(usePlayerStore.getState().isPlaying).toBe(false);
  }
  const preventDefault = mock(() => {});
  await act(async () =>
    keydown!({
      code: 'Space',
      target: { tagName: 'DIV', isContentEditable: false, closest: () => null },
      preventDefault,
      stopPropagation: () => {},
    } as unknown as KeyboardEvent),
  );
  expect(preventDefault).toHaveBeenCalledTimes(1);
  expect(usePlayerStore.getState().isPlaying).toBe(true);
});

test('seeded selection, version, stale-event, close and unmount sequences preserve source identity', async () => {
  for (const seed of [0x1a2b3c4d, 0x91e10da5, 0xc0ffee]) {
    let random = seed >>> 0;
    const next = () => {
      random ^= random << 13;
      random ^= random >>> 17;
      random ^= random << 5;
      return random >>> 0;
    };
    const reached = [0, 0, 0, 0, 0];
    const finish = mock(() => {});
    const sources = [
      { id: 'a', url: '/audio/version/a-original?revision=1' },
      { id: 'a', url: '/audio/version/a-edited?revision=2' },
      { id: 'b', url: '/audio/version/b-original?revision=1' },
    ];
    usePlayerStore.getState().reset();
    await mount();
    for (let step = 0; step < 50; step++) {
      const action = next() % 5;
      if (action === 0 || action === 4) {
        reached[action]++;
        const source = sources[next() % sources.length];
        await act(async () => {
          if (action === 4) {
            usePlayerStore.getState().setAudioWithAutoPlay(sources[0].url, 'a', null);
            usePlayerStore.getState().setAudioWithAutoPlay(sources[2].url, 'b', null);
          }
          usePlayerStore.getState().setAudioWithAutoPlay(source.url, source.id, null);
          usePlayerStore.getState().setOnFinish(finish);
        });
        expect(waves.at(-1)?.url).toBe(source.url);
        expect(usePlayerStore.getState()).toMatchObject({
          audioId: source.id,
          audioUrl: source.url,
          duration: 0,
          isPlaying: false,
        });
      } else if (action === 1 && usePlayerStore.getState().audioUrl) {
        reached[action]++;
        const current = waves.at(-1)!;
        await act(async () => current.ready(7));
        expect(usePlayerStore.getState()).toMatchObject({
          audioUrl: current.url,
          duration: 7,
          isPlaying: true,
          shouldAutoPlay: false,
        });
      } else if (action === 2 && waves.length) {
        const current = usePlayerStore.getState().audioUrl ? waves.at(-1) : undefined;
        const stale = waves.filter((wave) => wave !== current);
        if (stale.length) {
          reached[action]++;
          const old = stale[next() % stale.length];
          const { audioId, audioUrl, duration, currentTime, isPlaying, sourceGeneration } =
            usePlayerStore.getState();
          const finishCount = finish.mock.calls.length;
          await act(async () => {
            old.emit('ready');
            old.emit('play');
            old.emit('pause');
            old.emit('timeupdate', 42);
            old.emit('seeking', 42);
            old.emit('finish');
            old.emit('error', new Error('stale'));
            old.emit('loading', 50);
          });
          expect(usePlayerStore.getState()).toMatchObject({
            audioId,
            audioUrl,
            duration,
            currentTime,
            isPlaying,
            sourceGeneration,
          });
          expect(finish.mock.calls.length).toBe(finishCount);
        }
      } else if (action === 3) {
        reached[action]++;
        await act(async () => usePlayerStore.getState().reset());
        expect(usePlayerStore.getState()).toMatchObject({
          audioId: null,
          audioUrl: null,
          duration: 0,
          currentTime: 0,
          isPlaying: false,
          shouldAutoPlay: false,
        });
      }
      const selected = usePlayerStore.getState();
      if (selected.audioUrl) expect(waves.at(-1)?.url).toBe(selected.audioUrl);
    }
    expect(reached.every((count) => count > 0)).toBe(true);
    const last = waves.at(-1);
    const before = usePlayerStore.getState();
    await act(async () => renderer.unmount());
    if (last)
      await act(async () => {
        last.emit('play');
        last.emit('ready');
        last.emit('finish');
      });
    expect(usePlayerStore.getState()).toMatchObject({
      audioId: before.audioId,
      audioUrl: before.audioUrl,
      duration: before.duration,
      isPlaying: before.isPlaying,
    });
    waves.length = 0;
  }
});
