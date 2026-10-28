import { afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { createAudioKeepAlive } from '../src/lib/audio/audioKeepAlive';

const contexts: Context[] = [];
const elements: AudioElement[] = [];
let dispose: (() => Promise<void>) | undefined;

class Context {
  state: AudioContextState = 'suspended';
  destination = {};
  source = {
    offset: { value: 1 },
    connect: mock(() => {}),
    disconnect: mock(() => {}),
    start: mock(() => {}),
    stop: mock(() => {}),
  };
  track = { stop: mock(() => {}) };
  stream = { getTracks: () => [this.track] };
  output = { stream: this.stream };
  resume = mock(async () => {
    this.state = 'running';
  });
  close = mock(async () => {
    this.state = 'closed';
  });
  constructor() {
    contexts.push(this);
  }
  createConstantSource() {
    return this.source;
  }
  createMediaStreamDestination() {
    return this.output;
  }
}

class AudioElement {
  src = '';
  srcObject: unknown = null;
  loop = false;
  muted = false;
  volume = 1;
  paused = true;
  play = mock(async () => {
    this.paused = false;
  });
  pause = mock(() => {
    this.paused = true;
  });
  constructor() {
    elements.push(this);
  }
}

const originalContext = globalThis.AudioContext;
const originalAudio = globalThis.Audio;
beforeEach(() => {
  contexts.length = 0;
  elements.length = 0;
  Object.defineProperty(globalThis, 'AudioContext', { configurable: true, value: Context });
  Object.defineProperty(globalThis, 'Audio', { configurable: true, value: AudioElement });
});
afterEach(async () => {
  await dispose?.();
  dispose = undefined;
  Object.defineProperty(globalThis, 'AudioContext', { configurable: true, value: originalContext });
  Object.defineProperty(globalThis, 'Audio', { configurable: true, value: originalAudio });
});

function session() {
  const result = createAudioKeepAlive();
  dispose = result.dispose;
  return result;
}

test('keeps an unmuted media session alive without a finite clip or seek loop', async () => {
  const output = session();
  await output.start();
  const [ctx] = contexts;
  const [audio] = elements;
  expect(audio.loop).toBe(false);
  expect(audio.srcObject).toBe(ctx.stream);
  expect(audio.src).toBe('');
  expect(audio.muted).toBe(false);
  expect(audio.volume).toBe(1);
  expect(ctx.source.offset.value).toBe(0);
  expect(ctx.source.connect).toHaveBeenCalledWith(ctx.output);
  expect(ctx.source.start).toHaveBeenCalledTimes(1);
  expect(audio.paused).toBe(false);
});

test('retries blocked autoplay on a later gesture using the same session', async () => {
  const output = session();
  const [ctx] = contexts;
  const [audio] = elements;
  audio.play.mockImplementationOnce(async () => {
    throw new DOMException('User gesture required', 'NotAllowedError');
  });
  await expect(output.start()).rejects.toThrow('User gesture required');
  await output.start();
  expect(audio.paused).toBe(false);
  expect(contexts).toHaveLength(1);
  expect(elements).toHaveLength(1);
  expect(ctx.source.start).toHaveBeenCalledTimes(1);
});

test('requests resume and play within the gesture, even when an earlier resume is pending', async () => {
  const output = session();
  const [ctx] = contexts;
  const [audio] = elements;
  let release!: () => void;
  ctx.resume.mockImplementationOnce(
    () =>
      new Promise<void>((resolve) => {
        release = resolve;
      }),
  );
  const pending = output.start();
  expect(audio.play).toHaveBeenCalledTimes(1);
  await output.start();
  expect(ctx.resume).toHaveBeenCalledTimes(2);
  release();
  await pending;
});

test('recovers a suspended context even if the media element still reports playing', async () => {
  const output = session();
  await output.start();
  contexts[0].state = 'suspended';
  await output.start();
  expect(contexts[0].state).toBe('running');
  expect(contexts[0].resume).toHaveBeenCalledTimes(2);
  expect(elements[0].play).toHaveBeenCalledTimes(1);
});

test('recovers a paused element on wake without allocating another audio graph', async () => {
  const output = session();
  await output.start();
  elements[0].pause();
  await output.start();
  await output.start();
  expect(elements[0].paused).toBe(false);
  expect(elements[0].play).toHaveBeenCalledTimes(2);
  expect(contexts[0].resume).toHaveBeenCalledTimes(1);
  expect(contexts).toHaveLength(1);
});

test('disposal releases the media stream, source and context; later wake is inert', async () => {
  const output = session();
  await output.start();
  await output.dispose();
  await output.dispose();
  await output.start();
  expect(elements[0].paused).toBe(true);
  expect(elements[0].srcObject).toBeNull();
  expect(elements[0].play).toHaveBeenCalledTimes(1);
  expect(contexts[0].source.stop).toHaveBeenCalledTimes(1);
  expect(contexts[0].source.disconnect).toHaveBeenCalledTimes(1);
  expect(contexts[0].track.stop).toHaveBeenCalledTimes(1);
  expect(contexts[0].close).toHaveBeenCalledTimes(1);
});

test('disposal during pending startup does not attach or start another session', async () => {
  const output = session();
  let release!: () => void;
  contexts[0].resume.mockImplementationOnce(
    () =>
      new Promise<void>((resolve) => {
        release = resolve;
      }),
  );
  const pending = output.start();
  await output.dispose();
  release();
  await pending;
  await output.start();
  expect(elements[0].srcObject).toBeNull();
  expect(elements[0].paused).toBe(true);
  expect(contexts[0].close).toHaveBeenCalledTimes(1);
  expect(elements[0].play).toHaveBeenCalledTimes(1);
});
