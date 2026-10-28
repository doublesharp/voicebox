import { expect, test } from 'bun:test';
import { AudioMeter, heldLevel, samplePeak } from '../src/features/studio/meters';
import { StudioPlayback } from '../src/features/studio/playback';

test('playback exposes an independent idle meter subscription without starting audio', () => {
  const player = new StudioPlayback();
  expect(player.meters.getSnapshot().status).toBe('idle');
  let playbackChanges = 0;
  const unsubscribe = player.subscribe(() => {
    playbackChanges++;
  });
  player.meters.unavailable();
  expect(player.meters.getSnapshot().status).toBe('unavailable');
  expect(playbackChanges).toBe(0);
  expect(player.getSnapshot().mode).toBe('idle');
  unsubscribe();
  player.dispose();
});

test('sample peaks preserve stereo differences, silence, and over-range samples', () => {
  expect(samplePeak(new Float32Array([0, -0.5, 0.25]))).toBeCloseTo(-6.0206, 3);
  expect(samplePeak(new Float32Array([0, 0]))).toBe(-60);
  expect(samplePeak(new Float32Array([1.25]))).toBeGreaterThan(0);
  expect(samplePeak(new Float32Array([NaN, Infinity]))).toBe(-60);
});

test('hold decays at a bounded rate while sample clipping latches until reset', () => {
  const hit = heldLevel(0, { db: -12, holdDb: -12, clipped: false }, 0.05);
  const quiet = heldLevel(-30, hit, 0.5);
  expect(quiet.holdDb).toBe(-6);
  expect(quiet.clipped).toBe(true);
  const meter = new AudioMeter();
  meter.stop();
  expect(meter.getSnapshot().channels.every((channel) => !channel.clipped)).toBe(true);
});

test('analysis reads both graph channels, silences only its tap and disconnects every node', () => {
  const meter = new AudioMeter();
  const connections: unknown[][] = [];
  const nodes: Array<{ disconnected: boolean }> = [];
  let channel = 0;
  function node() {
    const record = { disconnected: false };
    nodes.push(record);
    return {
      gain: { value: 1 },
      connect: (...args: unknown[]) => connections.push(args),
      disconnect: () => {
        record.disconnected = true;
      },
    };
  }
  const context = {
    currentTime: 0,
    destination: {},
    createGain: node,
    createChannelSplitter: node,
    createAnalyser: () => {
      const value = channel++ === 0 ? 1 : 0.25;
      return { ...node(), getFloatTimeDomainData: (buffer: Float32Array) => buffer.fill(value) };
    },
  } as unknown as AudioContext;
  const input = meter.attach(context);
  expect(input).not.toBeNull();
  expect(meter.getSnapshot().status).toBe('active');
  expect(meter.getSnapshot().channels[0].clipped).toBe(true);
  expect(meter.getSnapshot().channels[1].db).toBeCloseTo(-12.0412, 3);
  expect(connections.some((args) => args[1] === 1)).toBe(true);
  meter.stop();
  expect(nodes.every((record) => record.disconnected)).toBe(true);
  expect(meter.getSnapshot().status).toBe('idle');
});

test('unsupported graph reports unavailable and disconnects its partial tap', () => {
  const meter = new AudioMeter();
  let disconnected = false;
  meter.attach({
    createGain: () => ({
      disconnect: () => {
        disconnected = true;
      },
    }),
  } as unknown as AudioContext);
  expect(meter.getSnapshot().status).toBe('unavailable');
  expect(disconnected).toBe(true);
});

test('hidden pages stop sampling, visibility resumes it, and stop cancels all refresh work', () => {
  const originalDocument = globalThis.document;
  const originalSet = globalThis.setTimeout;
  const originalClear = globalThis.clearTimeout;
  const callbacks = new Map<number, () => void>();
  let next = 0;
  let visibility: (() => void) | undefined;
  let reads = 0;
  const page = {
    hidden: false,
    addEventListener: (_name: string, callback: () => void) => {
      visibility = callback;
    },
    removeEventListener: () => {
      visibility = undefined;
    },
  };
  const meter = new AudioMeter();
  try {
    globalThis.document = page as unknown as Document;
    globalThis.setTimeout = ((callback: () => void) => {
      callbacks.set(++next, callback);
      return next;
    }) as unknown as typeof setTimeout;
    globalThis.clearTimeout = ((id: number) => {
      callbacks.delete(id);
    }) as unknown as typeof clearTimeout;
    const node = () => ({ gain: { value: 1 }, connect() {}, disconnect() {} });
    meter.attach({
      currentTime: 0,
      destination: {},
      createGain: node,
      createChannelSplitter: node,
      createAnalyser: () => ({
        ...node(),
        getFloatTimeDomainData: (buffer: Float32Array) => {
          reads++;
          buffer.fill(0);
        },
      }),
    } as unknown as AudioContext);
    expect(reads).toBe(2);
    expect(callbacks.size).toBe(1);
    page.hidden = true;
    visibility?.();
    expect(callbacks.size).toBe(0);
    expect(reads).toBe(2);
    expect(meter.getSnapshot().status).toBe('suspended');
    page.hidden = false;
    visibility?.();
    expect(reads).toBe(4);
    expect(callbacks.size).toBe(1);
    meter.stop();
    expect(callbacks.size).toBe(0);
    expect(visibility).toBeUndefined();
  } finally {
    meter.stop();
    globalThis.document = originalDocument;
    globalThis.setTimeout = originalSet;
    globalThis.clearTimeout = originalClear;
  }
});

test('audition uses one media-element graph and cancellation during resume cannot start late audio', async () => {
  const originalAudio = globalThis.Audio;
  const originalContext = globalThis.AudioContext;
  let release!: () => void;
  let enter!: () => void;
  const entered = new Promise<void>((resolve) => {
    enter = resolve;
  });
  let plays = 0;
  let closed = false;
  let mediaSources = 0;
  let sourceConnections = 0;
  class AudioStub {
    src = '';
    crossOrigin = '';
    pause() {}
    load() {}
    removeAttribute() {}
    async play() {
      plays++;
    }
  }
  const node = () => ({ gain: { value: 1 }, connect() {}, disconnect() {} });
  class ContextStub {
    currentTime = 0;
    destination = {};
    createGain = node;
    createChannelSplitter = node;
    createAnalyser = () => ({
      ...node(),
      getFloatTimeDomainData: (buffer: Float32Array) => buffer.fill(0.25),
    });
    createMediaElementSource(audio: AudioStub) {
      mediaSources++;
      expect(audio.crossOrigin).toBe('anonymous');
      return {
        connect() {
          sourceConnections++;
        },
      };
    }
    resume() {
      enter();
      return new Promise<void>((resolve) => {
        release = resolve;
      });
    }
    async close() {
      closed = true;
    }
  }
  const player = new StudioPlayback();
  try {
    globalThis.Audio = AudioStub as unknown as typeof Audio;
    globalThis.AudioContext = ContextStub as unknown as typeof AudioContext;
    const pending = player.audition('https://audio.example/take', 'Take');
    await entered;
    expect(mediaSources).toBe(1);
    expect(sourceConnections).toBe(2); // One audible route, one silent analysis tap.
    expect(player.meters.getSnapshot().channels[0].db).toBeCloseTo(-12.0412, 3);
    player.stop();
    release();
    await pending;
    expect(plays).toBe(0);
    expect(closed).toBe(true);
    expect(player.meters.getSnapshot().status).toBe('idle');
  } finally {
    player.dispose();
    globalThis.Audio = originalAudio;
    globalThis.AudioContext = originalContext;
  }
});
