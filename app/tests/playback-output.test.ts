import { afterAll, beforeEach, describe, expect, mock, test } from 'bun:test';

let releaseOutput: () => void = () => {};
let rejectOutput: (error: Error) => void = () => {};
const contexts: Context[] = [];
const disconnects: ReturnType<typeof mock>[] = [];
const start = mock(() => {});
const originalContext = Object.getOwnPropertyDescriptor(globalThis, 'AudioContext');
const originalAudio = Object.getOwnPropertyDescriptor(globalThis, 'Audio');
class Context {
  state: AudioContextState = 'suspended';
  sampleRate = 44100;
  currentTime = 0;
  destination = {};
  options: AudioContextOptions;
  constructor(options: AudioContextOptions) {
    this.options = options;
    contexts.push(this);
  }
  resume = mock(
    () =>
      new Promise<void>((resolve, reject) => {
        releaseOutput = () => {
          this.state = 'running';
          resolve();
        };
        rejectOutput = reject;
      }),
  );
  createMediaElementSource() {
    const disconnect = mock(() => {});
    disconnects.push(disconnect);
    return { connect: mock(() => {}), disconnect };
  }
  createGain() {
    return { gain: { value: 1 }, connect: mock(() => {}), disconnect: mock(() => {}) };
  }
}
class AudioElement extends EventTarget {
  src = '';
  preload = '';
  crossOrigin: string | null = null;
  paused = true;
  readyState = 4;
  play = mock(async () => {
    this.paused = false;
    start();
    this.dispatchEvent(new Event('play'));
  });
  pause = mock(() => {
    this.paused = true;
    this.dispatchEvent(new Event('pause'));
  });
  removeAttribute() {
    this.src = '';
  }
  load() {}
}
Object.defineProperty(globalThis, 'AudioContext', { configurable: true, value: Context });
Object.defineProperty(globalThis, 'Audio', { configurable: true, value: AudioElement });

let moduleId = 0;
async function freshOutput() {
  return import(`../src/lib/audio/playbackOutput.ts?case=${++moduleId}`);
}
beforeEach(() => {
  contexts.length = 0;
  disconnects.length = 0;
  start.mockClear();
  Object.defineProperty(globalThis, 'AudioContext', { configurable: true, value: Context });
});
afterAll(() => {
  if (originalContext) Object.defineProperty(globalThis, 'AudioContext', originalContext);
  else Reflect.deleteProperty(globalThis, 'AudioContext');
  if (originalAudio) Object.defineProperty(globalThis, 'Audio', originalAudio);
  else Reflect.deleteProperty(globalThis, 'Audio');
});

describe('audio output startup', () => {
  test('does not advance the clip before the output context has resumed', async () => {
    const { createPlaybackOutput } = await freshOutput();
    const output = createPlaybackOutput();
    const pending = output.audio.play();
    await Promise.resolve();
    expect(start).not.toHaveBeenCalled();
    releaseOutput();
    await pending;
    expect(start).toHaveBeenCalledTimes(1);
    output.dispose();
  });

  test('shares a device-rate context with a playback buffer between clips', async () => {
    const { createPlaybackOutput } = await freshOutput();
    const first = createPlaybackOutput();
    const second = createPlaybackOutput();
    expect(contexts).toHaveLength(1);
    expect(contexts[0].options).toEqual({ latencyHint: 'playback' });
    first.dispose();
    second.dispose();
  });

  test('canceling while output wakes does not play the canceled clip', async () => {
    const { createPlaybackOutput } = await freshOutput();
    const output = createPlaybackOutput();
    const pending = output.audio.play();
    output.audio.pause();
    releaseOutput();
    await pending;
    expect(start).not.toHaveBeenCalled();
    output.dispose();
  });

  test('disposing a player during startup releases it without starting audio', async () => {
    const { createPlaybackOutput } = await freshOutput();
    const output = createPlaybackOutput();
    const pending = output.audio.play();
    output.dispose();
    releaseOutput();
    await pending;
    expect(start).not.toHaveBeenCalled();
  });

  test('concurrent clips share one resume and cancel independently', async () => {
    const { createPlaybackOutput } = await freshOutput();
    const first = createPlaybackOutput();
    const second = createPlaybackOutput();
    const pending = [first.audio.play(), second.audio.play()];
    expect(contexts[0].resume).toHaveBeenCalledTimes(1);
    first.audio.pause();
    releaseOutput();
    await Promise.all(pending);
    expect(start).toHaveBeenCalledTimes(1);
    expect(first.audio.paused).toBe(true);
    expect(second.audio.paused).toBe(false);
    first.dispose();
    second.dispose();
  });

  test('a later start on the same clip supersedes its canceled start', async () => {
    const { createPlaybackOutput } = await freshOutput();
    const output = createPlaybackOutput();
    const first = output.audio.play();
    output.audio.pause();
    const second = output.audio.play();
    releaseOutput();
    await Promise.all([first, second]);
    expect(start).toHaveBeenCalledTimes(1);
    expect(output.isStarting()).toBe(false);
    output.dispose();
  });

  test('resume rejection is reported and a later gesture can retry', async () => {
    const { createPlaybackOutput } = await freshOutput();
    const output = createPlaybackOutput();
    const pending = output.audio.play();
    rejectOutput(new Error('Resume denied'));
    await expect(pending).rejects.toThrow('Resume denied');
    expect(output.isStarting()).toBe(false);
    expect(start).not.toHaveBeenCalled();
    const retry = output.audio.play();
    releaseOutput();
    await retry;
    expect(start).toHaveBeenCalledTimes(1);
    output.dispose();
  });

  test('a disposed clip ignores a late resume rejection and disconnects once', async () => {
    const { createPlaybackOutput } = await freshOutput();
    const output = createPlaybackOutput();
    const pending = output.audio.play();
    output.dispose();
    output.dispose();
    rejectOutput(new Error('Late failure'));
    await pending;
    await output.audio.play();
    expect(start).not.toHaveBeenCalled();
    expect(disconnects[0]).toHaveBeenCalledTimes(1);
    expect(output.isStarting()).toBe(false);
  });

  test('an already running context starts media within the current gesture', async () => {
    const { createPlaybackOutput } = await freshOutput();
    const output = createPlaybackOutput();
    contexts[0].state = 'running';
    const pending = output.audio.play();
    expect(start).toHaveBeenCalledTimes(1);
    await pending;
    expect(contexts[0].resume).not.toHaveBeenCalled();
    output.dispose();
  });

  test('falls back to native media when Web Audio is unavailable', async () => {
    Reflect.deleteProperty(globalThis, 'AudioContext');
    const { createPlaybackOutput } = await freshOutput();
    const output = createPlaybackOutput();
    await output.audio.play();
    expect(start).toHaveBeenCalledTimes(1);
    expect(contexts).toHaveLength(0);
    output.dispose();
    expect(output.audio.paused).toBe(true);
  });
});
