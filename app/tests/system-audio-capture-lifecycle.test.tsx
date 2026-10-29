import { afterAll, afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
const timers: ReturnType<typeof setInterval>[] = [];
const ticks: (() => void)[] = [];
Object.defineProperty(globalThis, 'window', {
  configurable: true,
  value: {
    setInterval: (callback: () => void, ms: number) => {
      ticks.push(callback);
      const timer = setInterval(callback, ms);
      timers.push(timer);
      return timer;
    },
    clearInterval,
  },
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

const start = mock(async (_seconds: number) => {});
const stop = mock(async () => new Blob(['audio'], { type: 'audio/wav' }));
const supported = mock(async () => true);
const complete = mock((_blob: Blob, _duration?: number) => {});
const platform = {
  metadata: { isTauri: true },
  audio: {
    isSystemAudioSupported: supported,
    startSystemAudioCapture: start,
    stopSystemAudioCapture: stop,
  },
};
mock.module('../src/platform/PlatformContext', () => ({ usePlatform: () => platform }));
const { useSystemAudioCapture } = await import('../src/lib/hooks/useSystemAudioCapture');
let hook!: ReturnType<typeof useSystemAudioCapture>;
let view: ReactTestRenderer | undefined;
function Probe() {
  hook = useSystemAudioCapture({ onRecordingComplete: complete });
  return null;
}
async function mount() {
  await act(async () => {
    view = create(<Probe />);
  });
}
beforeEach(() => {
  start.mockReset();
  stop.mockReset();
  supported.mockReset();
  complete.mockClear();
  platform.metadata.isTauri = true;
  start.mockImplementation(async () => {});
  stop.mockImplementation(async () => new Blob(['audio'], { type: 'audio/wav' }));
  supported.mockImplementation(async () => true);
});
afterEach(async () => {
  if (view) await act(async () => view?.unmount());
  view = undefined;
  ticks.length = 0;
  for (const timer of timers.splice(0)) clearInterval(timer);
});
afterAll(() => {
  mock.restore();
  if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow);
  else Reflect.deleteProperty(globalThis, 'window');
});

test('cancelling a recording stops capture without publishing a sample', async () => {
  await mount();
  await act(async () => {
    await hook.startRecording();
  });
  expect(hook.isRecording).toBe(true);
  await act(async () => {
    await hook.cancelRecording();
  });
  expect(stop).toHaveBeenCalledTimes(1);
  expect(complete).not.toHaveBeenCalled();
  expect(hook.isRecording).toBe(false);
});

test('unmount during native startup stops the eventual capture without publishing it', async () => {
  const pending = deferred<void>();
  start.mockImplementation(() => pending.promise);
  await mount();
  let started!: Promise<void>;
  act(() => {
    started = hook.startRecording();
  });
  await act(async () => {
    view?.unmount();
  });
  view = undefined;
  await act(async () => {
    pending.resolve();
    await started;
  });
  expect(start).toHaveBeenCalledTimes(1);
  expect(stop).toHaveBeenCalledTimes(1);
  expect(complete).not.toHaveBeenCalled();
});

test('cancel during startup stops the eventual capture and does not publish it', async () => {
  const pending = deferred<void>();
  start.mockImplementation(() => pending.promise);
  await mount();
  let started!: Promise<void>;
  let cancelled!: Promise<void>;
  act(() => {
    started = hook.startRecording();
    cancelled = hook.cancelRecording();
  });
  await act(async () => {
    pending.resolve();
    await Promise.all([started, cancelled]);
  });
  expect(stop).toHaveBeenCalledTimes(1);
  expect(complete).not.toHaveBeenCalled();
  expect(hook.isRecording).toBe(false);
});

test('concurrent start requests start only one native capture', async () => {
  const pending = deferred<void>();
  start.mockImplementation(() => pending.promise);
  await mount();
  let first!: Promise<void>;
  let second!: Promise<void>;
  act(() => {
    first = hook.startRecording();
    second = hook.startRecording();
  });
  await act(async () => {
    pending.resolve();
    await Promise.all([first, second]);
  });
  expect(start).toHaveBeenCalledTimes(1);
  await act(async () => {
    await hook.cancelRecording();
  });
});

test('stop is idempotent and reports capture duration before native encoding finishes', async () => {
  const originalNow = Date.now;
  let now = 1000;
  Date.now = () => now;
  const pending = deferred<Blob>();
  stop.mockImplementation(() => pending.promise);
  try {
    await mount();
    await act(async () => {
      await hook.startRecording();
    });
    now = 3500;
    let first!: Promise<void>;
    let second!: Promise<void>;
    act(() => {
      first = hook.stopRecording();
      second = hook.stopRecording();
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(stop).toHaveBeenCalledTimes(1);
    now = 12000;
    const audio = new Blob(['audio']);
    await act(async () => {
      pending.resolve(audio);
      await Promise.all([first, second]);
    });
    expect(complete).toHaveBeenCalledTimes(1);
    expect(complete).toHaveBeenCalledWith(audio, 2.5);
    expect(hook.isRecording).toBe(false);
  } finally {
    Date.now = originalNow;
  }
});

test('cancel while stop is in flight discards the result and blocks a new start', async () => {
  const pending = deferred<Blob>();
  stop.mockImplementation(() => pending.promise);
  await mount();
  await act(async () => {
    await hook.startRecording();
  });
  let stopping!: Promise<void>;
  let cancelling!: Promise<void>;
  act(() => {
    stopping = hook.stopRecording();
    cancelling = hook.cancelRecording();
  });
  await act(async () => {
    await hook.startRecording();
  });
  expect(start).toHaveBeenCalledTimes(1);
  await act(async () => {
    pending.resolve(new Blob(['audio']));
    await Promise.all([stopping, cancelling]);
  });
  expect(stop).toHaveBeenCalledTimes(1);
  expect(complete).not.toHaveBeenCalled();
});

test('native startup failure is reported and allows a later retry', async () => {
  start.mockImplementationOnce(async () => {
    throw new Error('permission denied');
  });
  await mount();
  await act(async () => {
    await hook.startRecording();
  });
  expect(hook.error).toBe('permission denied');
  expect(hook.isRecording).toBe(false);
  expect(stop).not.toHaveBeenCalled();
  await act(async () => {
    await hook.startRecording();
  });
  expect(hook.isRecording).toBe(true);
  expect(hook.error).toBeNull();
  await act(async () => {
    await hook.cancelRecording();
  });
});

test('unsupported capture does not call the native start', async () => {
  supported.mockImplementation(async () => false);
  await mount();
  await act(async () => {
    await hook.startRecording();
  });
  expect(start).not.toHaveBeenCalled();
  expect(hook.error).toContain('not supported');
});

test('support probe failure leaves capture disabled', async () => {
  supported.mockImplementation(async () => {
    throw new Error('probe failed');
  });
  await mount();
  expect(hook.isSupported).toBe(false);
  await act(async () => {
    await hook.startRecording();
  });
  expect(start).not.toHaveBeenCalled();
  expect(hook.error).toContain('not supported');
});

test('duration limit stops once and publishes the result', async () => {
  const originalNow = Date.now;
  let now = 1000;
  Date.now = () => now;
  try {
    await mount();
    await act(async () => {
      await hook.startRecording();
    });
    now = 31000;
    await act(async () => {
      ticks[0]();
      ticks[0]();
      await Promise.resolve();
    });
    expect(stop).toHaveBeenCalledTimes(1);
    expect(complete).toHaveBeenCalledTimes(1);
    expect(hook.isRecording).toBe(false);
  } finally {
    Date.now = originalNow;
  }
});

test('unmount during native stop discards its result', async () => {
  const pending = deferred<Blob>();
  stop.mockImplementation(() => pending.promise);
  await mount();
  await act(async () => {
    await hook.startRecording();
  });
  let stopping!: Promise<void>;
  act(() => {
    stopping = hook.stopRecording();
  });
  await act(async () => {
    view?.unmount();
  });
  view = undefined;
  await act(async () => {
    pending.resolve(new Blob(['audio']));
    await stopping;
  });
  expect(stop).toHaveBeenCalledTimes(1);
  expect(complete).not.toHaveBeenCalled();
});

test('stop errors are reported and a later capture can start', async () => {
  stop.mockImplementationOnce(async () => {
    throw new Error('native stop failed');
  });
  await mount();
  await act(async () => {
    await hook.startRecording();
  });
  await act(async () => {
    await hook.stopRecording();
  });
  expect(hook.error).toBe('native stop failed');
  expect(hook.isRecording).toBe(false);
  await act(async () => {
    await hook.startRecording();
  });
  expect(hook.isRecording).toBe(true);
  await act(async () => {
    await hook.cancelRecording();
  });
});

test('web mode does not attempt native capture', async () => {
  platform.metadata.isTauri = false;
  await mount();
  await act(async () => {
    await hook.startRecording();
  });
  expect(start).not.toHaveBeenCalled();
  expect(hook.error).toContain('desktop app');
});
