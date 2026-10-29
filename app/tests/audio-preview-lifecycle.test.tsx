import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const previews: ReturnType<typeof output>[] = [];
const toasts: unknown[] = [];
const revoked: string[] = [];
const originalCreate = URL.createObjectURL;
const originalRevoke = URL.revokeObjectURL;
URL.createObjectURL = () => `blob:preview-${previews.length}`;
URL.revokeObjectURL = (url) => revoked.push(url);

function output() {
  let resolve!: () => void;
  let reject!: (error: Error) => void;
  let starting = false;
  const pending = new Promise<void>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  const audio = new EventTarget() as EventTarget & {
    src: string;
    paused: boolean;
    play: ReturnType<typeof mock>;
    pause: ReturnType<typeof mock>;
  };
  audio.src = '';
  audio.paused = true;
  audio.play = mock(() => {
    starting = true;
    return pending;
  });
  audio.pause = mock(() => {
    starting = false;
    audio.paused = true;
  });
  const dispose = mock(() => {
    starting = false;
    audio.paused = true;
  });
  return { audio, isStarting: () => starting, dispose, resolve, reject };
}
mock.module('../src/lib/audio/playbackOutput', () => ({
  createPlaybackOutput: () => {
    const preview = output();
    previews.push(preview);
    return preview;
  },
}));
mock.module('../src/components/ui/use-toast', () => ({
  useToast: () => ({ toast: (message: unknown) => toasts.push(message) }),
}));
const { useAudioPlayer } = await import('../src/lib/hooks/useAudioPlayer');
let hook!: ReturnType<typeof useAudioPlayer>;
let view: ReactTestRenderer | undefined;
function Harness() {
  hook = useAudioPlayer();
  return null;
}
function mount() {
  act(() => {
    view = create(<Harness />);
  });
}
afterEach(() => {
  act(() => view?.unmount());
  view = undefined;
  previews.length = 0;
  toasts.length = 0;
  revoked.length = 0;
});
afterAll(() => {
  mock.restore();
  URL.createObjectURL = originalCreate;
  URL.revokeObjectURL = originalRevoke;
});

test('cleanup and unmount dispose outputs and revoke preview URLs exactly once', () => {
  mount();
  act(() => hook.playPause(new File(['a'], 'a.wav')));
  const first = previews[0];
  act(() => hook.cleanup());
  expect(first.dispose).toHaveBeenCalledTimes(1);
  expect(revoked).toEqual(['blob:preview-1']);
  expect(hook.isPlaying).toBe(false);
  act(() => hook.playPause(new File(['b'], 'b.wav')));
  const second = previews[1];
  act(() => view?.unmount());
  view = undefined;
  expect(first.dispose).toHaveBeenCalledTimes(1);
  expect(second.dispose).toHaveBeenCalledTimes(1);
  expect(revoked).toEqual(['blob:preview-1', 'blob:preview-2']);
});

test('replacing a file retires its pending output and ignores its late failure', async () => {
  mount();
  act(() => hook.playPause(new File(['a'], 'a.wav')));
  const old = previews[0];
  act(() => hook.playPause(new File(['b'], 'b.wav')));
  expect(old.dispose).toHaveBeenCalledTimes(1);
  await act(async () => old.reject(new Error('Old failure')));
  expect(hook.isPlaying).toBe(true);
  expect(previews[1].dispose).not.toHaveBeenCalled();
  expect(toasts).toHaveLength(0);
});

test('pausing a pending start cancels it and ignores its later rejection', async () => {
  mount();
  const file = new File(['a'], 'a.wav');
  act(() => hook.playPause(file));
  const preview = previews[0];
  act(() => hook.playPause(file));
  expect(preview.audio.pause).toHaveBeenCalledTimes(1);
  await act(async () => preview.reject(new Error('Canceled start')));
  expect(hook.isPlaying).toBe(false);
  expect(toasts).toHaveLength(0);
});

test('a current play failure releases its resources and reports the error', async () => {
  mount();
  act(() => hook.playPause(new File(['a'], 'a.wav')));
  await act(async () => previews[0].reject(new Error('Autoplay denied')));
  expect(hook.isPlaying).toBe(false);
  expect(previews[0].dispose).toHaveBeenCalledTimes(1);
  expect(revoked).toHaveLength(1);
  expect(toasts).toHaveLength(1);
});

test('ended and error events release only their own preview', () => {
  mount();
  act(() => hook.playPause(new File(['a'], 'a.wav')));
  const old = previews[0];
  act(() => old.audio.dispatchEvent(new Event('ended')));
  expect(hook.isPlaying).toBe(false);
  expect(old.dispose).toHaveBeenCalledTimes(1);
  act(() => hook.playPause(new File(['b'], 'b.wav')));
  act(() => old.audio.dispatchEvent(new Event('error')));
  expect(hook.isPlaying).toBe(true);
  expect(toasts).toHaveLength(0);
  act(() => previews[1].audio.dispatchEvent(new Event('error')));
  expect(hook.isPlaying).toBe(false);
  expect(previews[1].dispose).toHaveBeenCalledTimes(1);
  expect(toasts).toHaveLength(1);
});
