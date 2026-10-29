import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import type { ButtonHTMLAttributes } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import type { NativePlaybackEvent } from '../src/platform/types';
import { usePlayerStore } from '../src/stores/playerStore';

const actualQuery = await import('@tanstack/react-query');
const actualPlatform = await import('../src/platform/PlatformContext');
const actualWaveSurfer = await import('wavesurfer.js');
const actualSlider = await import('../src/components/ui/slider');
const actualButton = await import('../src/components/ui/button');
const listeners = new Map<string, ((...args: unknown[]) => void)[]>();
let playing = false;
let currentTime = 0;
let nativeListener: (event: NativePlaybackEvent) => void = () => {};
const play = mock(async () => {
  playing = true;
  listeners.get('play')?.forEach((fn) => {
    fn();
  });
});
const wave = {
  on: (name: string, listener: (...args: unknown[]) => void) => {
    listeners.set(name, [...(listeners.get(name) ?? []), listener]);
    return () => {};
  },
  load: async () => {
    listeners.get('ready')?.forEach((fn) => {
      fn();
    });
  },
  getDuration: () => 5,
  getCurrentTime: () => currentTime,
  getMediaElement: () => ({}),
  setVolume: () => {},
  setMuted: () => {},
  seekTo: (progress: number) => {
    wave.setTime(progress * 5);
  },
  setTime: (time: number) => {
    currentTime = time;
    for (const listener of listeners.get('seeking') ?? []) listener(time);
    for (const listener of listeners.get('timeupdate') ?? []) listener(time);
  },
  isPlaying: () => playing,
  play,
  pause: () => {
    if (playing) {
      playing = false;
      listeners.get('pause')?.forEach((fn) => {
        fn();
      });
    }
  },
  destroy: () => {},
};
let resolveStart: (value: { session_id: string; duration: number; position: number }) => void;
const playToDevices = mock(
  () =>
    new Promise((resolve) => {
      resolveStart = resolve;
    }),
);
const stopPlayback = mock(async (_sessionId?: string) => {});
const controlPlayback = mock(async (_sessionId: string, _control: unknown) => {});
const platform = {
  metadata: { isTauri: true },
  audio: {
    playToDevices,
    stopPlayback,
    controlPlayback,
    subscribeToPlayback: async (callback: typeof nativeListener) => {
      nativeListener = callback;
      return () => {};
    },
  },
};
mock.module('@tanstack/react-query', () => ({
  ...actualQuery,
  useQuery: ({ queryKey }: { queryKey: string[] }) => ({
    data:
      queryKey[0] === 'channels'
        ? [{ id: 'route', is_default: false, device_ids: ['device_speakers'] }]
        : { channel_ids: ['route'] },
  }),
}));
mock.module('../src/platform/PlatformContext', () => ({
  ...actualPlatform,
  usePlatform: () => platform,
}));
mock.module('wavesurfer.js', () => ({ ...actualWaveSurfer, default: { create: () => wave } }));
mock.module('../src/components/ui/slider', () => ({
  Slider: (props: Record<string, unknown>) => <div {...props} />,
}));
mock.module('../src/components/ui/button', () => ({
  Button: (props: ButtonHTMLAttributes<HTMLButtonElement>) => <button {...props} />,
}));
const originalFetch = globalThis.fetch;
const originalDocument = globalThis.document;
const originalWindow = globalThis.window;
const originalRaf = globalThis.requestAnimationFrame;
const originalCancel = globalThis.cancelAnimationFrame;
const originalStyle = globalThis.getComputedStyle;
globalThis.fetch = mock(async () => new Response(new Uint8Array(4))) as unknown as typeof fetch;
globalThis.document = {
  documentElement: {},
  addEventListener: () => {},
  removeEventListener: () => {},
} as unknown as Document;
const style = { display: 'block', visibility: 'visible', getPropertyValue: () => '0 0% 100%' };
globalThis.window = { getComputedStyle: () => style } as unknown as Window & typeof globalThis;
globalThis.getComputedStyle = () => style as unknown as CSSStyleDeclaration;
globalThis.requestAnimationFrame = (callback: FrameRequestCallback) => {
  queueMicrotask(() => callback(0));
  return 1;
};
globalThis.cancelAnimationFrame = () => {};
const { AudioPlayer } = await import('../src/components/AudioPlayer/AudioPlayer');
let renderer: ReactTestRenderer | undefined;
afterEach(async () => {
  if (renderer) {
    await act(async () => renderer?.unmount());
    renderer = undefined;
  }
  usePlayerStore.getState().reset();
  listeners.clear();
  playing = false;
  currentTime = 0;
  nativeListener = () => {};
  controlPlayback.mockClear();
  playToDevices.mockImplementation(
    () =>
      new Promise((resolve) => {
        resolveStart = resolve;
      }),
  );
  play.mockClear();
  playToDevices.mockClear();
  stopPlayback.mockClear();
});
afterAll(() => {
  globalThis.fetch = originalFetch;
  globalThis.document = originalDocument;
  globalThis.window = originalWindow;
  globalThis.requestAnimationFrame = originalRaf;
  globalThis.cancelAnimationFrame = originalCancel;
  globalThis.getComputedStyle = originalStyle;
  mock.module('@tanstack/react-query', () => actualQuery);
  mock.module('../src/platform/PlatformContext', () => actualPlatform);
  mock.module('wavesurfer.js', () => actualWaveSurfer);
  mock.module('../src/components/ui/slider', () => actualSlider);
  mock.module('../src/components/ui/button', () => actualButton);
});

test('closing while native startup is pending cancels its returned session without restarting playback', async () => {
  usePlayerStore.getState().setAudio('/audio/test', 'audio', 'profile');
  await act(async () => {
    renderer = create(<AudioPlayer />, {
      createNodeMock: () => ({ getBoundingClientRect: () => ({ width: 800, height: 80 }) }),
    });
  });
  const button = (label: string) =>
    renderer!.root.findAllByType('button').find((item) => item.props['aria-label'] === label)!;
  let start: Promise<void> | undefined;
  await act(async () => {
    start = button('Play').props.onClick();
    await Promise.resolve();
  });
  expect(playToDevices).toHaveBeenCalledTimes(1);
  await act(async () => {
    button('Close player').props.onClick();
  });
  await act(async () => {
    resolveStart({ session_id: 'pending-session', duration: 5, position: 0 });
    await start;
  });
  expect(stopPlayback).toHaveBeenCalledWith('pending-session');
  expect(play).not.toHaveBeenCalled();
  expect(usePlayerStore.getState().isPlaying).toBe(false);
});

test('native acknowledgement starts controls immediately and native events own position, pause, seek and volume', async () => {
  playToDevices.mockImplementation(async () => ({
    session_id: 'active',
    duration: 5,
    position: 0,
  }));
  usePlayerStore.getState().setAudio('/audio/test', 'audio', 'profile');
  await act(async () => {
    renderer = create(<AudioPlayer />, {
      createNodeMock: () => ({ getBoundingClientRect: () => ({ width: 800, height: 80 }) }),
    });
  });
  const button = (label: string) =>
    renderer!.root.findAllByType('button').find((item) => item.props['aria-label'] === label)!;
  await act(async () => {
    await button('Play').props.onClick();
  });
  expect(usePlayerStore.getState().isPlaying).toBe(true);
  expect(play).not.toHaveBeenCalled();
  await act(async () => {
    nativeListener({ session_id: 'active', state: 'playing', position: 1.25, duration: 5 });
  });
  expect(usePlayerStore.getState().currentTime).toBe(1.25);
  await act(async () => {
    await button('Pause').props.onClick();
  });
  expect(controlPlayback).toHaveBeenCalledWith('active', { paused: true });
  expect(usePlayerStore.getState().isPlaying).toBe(false);
  await act(async () => {
    renderer!.root.findByProps({ 'aria-label': 'Playback position' }).props.onValueChange([50]);
  });
  expect(controlPlayback).toHaveBeenCalledWith('active', { position: 2.5 });
  await act(async () => {
    usePlayerStore.getState().setVolume(0);
  });
  expect(controlPlayback).toHaveBeenCalledWith('active', { volume: 0 });
  await act(async () => {
    button('Close player').props.onClick();
  });
  expect(stopPlayback).toHaveBeenCalledWith('active');
});

test('autoplay respects native routing and native completion advances only once', async () => {
  playToDevices.mockImplementation(async () => ({
    session_id: 'autoplay',
    duration: 5,
    position: 0,
  }));
  const finish = mock(() => {});
  usePlayerStore.getState().setAudioWithAutoPlay('/audio/test', 'audio', 'profile');
  usePlayerStore.getState().setOnFinish(finish);
  await act(async () => {
    renderer = create(<AudioPlayer />, {
      createNodeMock: () => ({ getBoundingClientRect: () => ({ width: 800, height: 80 }) }),
    });
  });
  expect(playToDevices).toHaveBeenCalledTimes(1);
  expect(play).not.toHaveBeenCalled();
  await act(async () => {
    nativeListener({ session_id: 'autoplay', state: 'ended', position: 5, duration: 5 });
  });
  expect(finish).toHaveBeenCalledTimes(1);
  expect(usePlayerStore.getState().isPlaying).toBe(false);
});

test('a delayed native pause cannot change a later selection of the same clip', async () => {
  let session = 0;
  playToDevices.mockImplementation(async () => ({
    session_id: `selection-${++session}`,
    duration: 5,
    position: 0,
  }));
  usePlayerStore.getState().setAudioWithAutoPlay('/audio/a', 'a', 'profile');
  await act(async () => {
    renderer = create(<AudioPlayer />, {
      createNodeMock: () => ({ getBoundingClientRect: () => ({ width: 800, height: 80 }) }),
    });
  });
  let resolvePause!: () => void;
  controlPlayback.mockImplementationOnce(
    () =>
      new Promise<void>((resolve) => {
        resolvePause = resolve;
      }),
  );
  let pause: Promise<void>;
  await act(async () => {
    pause = renderer!.root
      .findAllByType('button')
      .find((node) => node.props['aria-label'] === 'Pause')!
      .props.onClick();
    await Promise.resolve();
  });
  await act(async () => {
    usePlayerStore.getState().setAudioWithAutoPlay('/audio/b', 'b', 'profile');
  });
  await act(async () => {
    usePlayerStore.getState().setAudioWithAutoPlay('/audio/a', 'a', 'profile');
  });
  expect(usePlayerStore.getState().isPlaying).toBe(true);
  await act(async () => {
    resolvePause();
    await pause;
  });
  expect(usePlayerStore.getState()).toMatchObject({ audioId: 'a', isPlaying: true, duration: 5 });
});
