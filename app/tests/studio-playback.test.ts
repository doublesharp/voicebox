import { afterEach, describe, expect, test } from 'bun:test';
import { StudioPlayback } from '../src/features/studio/playback';
import type { StudioAsset, StudioDocument } from '../src/features/studio/types';

const originalAudio = globalThis.Audio;
const originalContext = globalThis.AudioContext;
const originalFetch = globalThis.fetch;
const players: StudioPlayback[] = [];
class FakeAudio {
  static instances: FakeAudio[] = [];
  static start: () => Promise<void> = async () => {};
  paused = true;
  disposed = false;
  currentTime = 0;
  duration = 2;
  preload = '';
  onended: (() => void) | null = null;
  ontimeupdate: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(readonly src: string) {
    FakeAudio.instances.push(this);
  }
  async play() {
    await FakeAudio.start();
    this.paused = false;
  }
  pause() {
    this.paused = true;
  }
  removeAttribute() {
    this.disposed = true;
  }
  load() {}
}
class FakeContext {
  static instances: FakeContext[] = [];
  static decode: () => Promise<unknown> = async () => ({ duration: 3 });
  currentTime = 0;
  destination = {};
  closed = false;
  sources: Array<{ start: number[]; stopped: boolean }> = [];
  envelopes: Float32Array[] = [];
  constructor() {
    FakeContext.instances.push(this);
  }
  async resume() {}
  async close() {
    this.closed = true;
  }
  decodeAudioData() {
    return FakeContext.decode();
  }
  createBufferSource() {
    const record = { start: [] as number[], stopped: false };
    this.sources.push(record);
    return {
      buffer: null,
      connect() {},
      disconnect() {},
      start(...args: number[]) {
        record.start = args;
      },
      stop() {
        record.stopped = true;
      },
    };
  }
  createGain() {
    return {
      gain: {
        setValueCurveAtTime: (values: Float32Array) => {
          this.envelopes.push(values);
        },
      },
      connect() {},
      disconnect() {},
    };
  }
}
function setup() {
  FakeAudio.instances = [];
  FakeContext.instances = [];
  FakeAudio.start = async () => {};
  FakeContext.decode = async () => ({ duration: 3 });
  globalThis.Audio = FakeAudio as unknown as typeof Audio;
  globalThis.AudioContext = FakeContext as unknown as typeof AudioContext;
  globalThis.fetch = (async () => new Response(new Uint8Array([0]))) as typeof fetch;
  const player = new StudioPlayback();
  players.push(player);
  return player;
}
afterEach(() => {
  for (const player of players.splice(0)) player.dispose();
  globalThis.Audio = originalAudio;
  globalThis.AudioContext = originalContext;
  globalThis.fetch = originalFetch;
});
const assets = [
  { id: 'asset', sample_rate: 24000, frames: 72000, duration: 3, channels: 1 },
] as StudioAsset[];
const document = {
  sample_rate: 48000,
  channels: 2,
  tracks: [{ id: 'voice', gain: 0.5, mute: false, solo: false }],
  clips: [
    {
      id: 'clip',
      track_id: 'voice',
      asset_id: 'asset',
      start_sample: 48000,
      trim_start_sample: 12000,
      trim_end_sample: 24000,
      gain: 0.8,
      fade_in_samples: 4800,
      fade_out_samples: 4800,
    },
  ],
} as StudioDocument;

describe('studio exclusive playback boundary', () => {
  test('a new audition stops and releases its previous source; dispose leaves no hidden playback', async () => {
    const player = setup();
    await player.audition('/take-a', 'A');
    await player.audition('/encoded-file', 'Actual deliverable');
    expect(FakeAudio.instances[0].paused).toBe(true);
    expect(FakeAudio.instances[0].disposed).toBe(true);
    expect(player.getSnapshot().label).toBe('Actual deliverable');
    player.dispose();
    expect(FakeAudio.instances[1].paused).toBe(true);
    expect(FakeAudio.instances[1].disposed).toBe(true);
    expect(player.getSnapshot().mode).toBe('idle');
  });
  test('timeline schedules native-rate sources using project sample trims and fixed starts', async () => {
    const player = setup();
    await player.timeline(document, assets, (id) => `/source/${id}`, 0);
    const context = FakeContext.instances[0];
    expect(context.sources[0].start).toEqual([1.04, 0, 1]);
    expect(context.envelopes[0][0]).toBeCloseTo(0, 7);
    expect(Math.max(...context.envelopes[0])).toBeCloseTo(0.4, 6);
    expect(context.envelopes[0][context.envelopes[0].length - 1]).toBeCloseTo(0.4, 6);
    await player.audition('/take', 'Take');
    expect(context.closed).toBe(true);
    expect(context.sources[0].stopped).toBe(true);
  });
  test('stop during decoding prevents a late timeline start', async () => {
    const player = setup();
    let release!: () => void;
    let decoding!: () => void;
    const entered = new Promise<void>((resolve) => {
      decoding = resolve;
    });
    FakeContext.decode = () => {
      decoding();
      return new Promise((resolve) => {
        release = () => resolve({ duration: 3 });
      });
    };
    const starting = player.timeline(document, assets, (id) => `/source/${id}`, 0);
    await entered;
    player.stop();
    release();
    await starting;
    expect(FakeContext.instances[0].sources).toHaveLength(0);
    expect(player.getSnapshot().mode).toBe('idle');
    expect(player.getSnapshot().error).toBeNull();
  });
  test('failure to stop another playback owner is visible and cannot start competing audio', async () => {
    setup();
    const player = new StudioPlayback(() => {
      throw new Error('Native output did not stop');
    });
    players.push(player);
    await player.audition('/take', 'Take');
    expect(player.getSnapshot().error).toContain('Native output did not stop');
    expect(FakeAudio.instances).toHaveLength(0);
  });
});

test('waits for native stop before beginning an audition', async () => {
  setup();
  let release!: () => void;
  const stopped = new Promise<void>((resolve) => {
    release = resolve;
  });
  const player = new StudioPlayback(() => stopped);
  players.push(player);
  const pending = player.audition('/take', 'Take');
  await Promise.resolve();
  expect(FakeAudio.instances).toHaveLength(0);
  release();
  await pending;
  expect(FakeAudio.instances).toHaveLength(1);
});

test('timeline requests bounded preview ranges instead of entire source files', async () => {
  const player = setup();
  const urls: string[] = [];
  globalThis.fetch = (async (url) => {
    urls.push(String(url));
    return new Response(new Uint8Array([0]));
  }) as typeof fetch;
  await player.timeline(document, assets, (id) => `http://local/assets/${id}/audio`, 0);
  expect(urls[0]).toContain('/preview?');
  expect(urls[0]).toContain('start_sample=12000');
  expect(urls[0]).toContain('frames=48000');
});

test('ownership waits for an in-flight native start and stops it after revocation', async () => {
  const { legacyPlaybackToken, startNative, stopNative } = await import(
    '../src/lib/audio/playbackOwnership'
  );
  const token = legacyPlaybackToken();
  let release!: () => void;
  let entered!: () => void;
  const ready = new Promise<void>((resolve) => {
    entered = resolve;
  });
  const events: string[] = [];
  const start = startNative(
    token,
    async () => {
      events.push('start');
      entered();
      await new Promise<void>((resolve) => {
        release = resolve;
      });
    },
    async () => {
      events.push('late-stop');
    },
  );
  await ready;
  const stop = stopNative(async () => {
    events.push('claim-stop');
  });
  release();
  await Promise.all([start, stop]);
  expect(events).toEqual(['start', 'late-stop', 'claim-stop']);
});

test('ownership aborts a pending legacy fetch before it can start', async () => {
  const { legacyPlaybackToken, stopNative } = await import('../src/lib/audio/playbackOwnership');
  const token = legacyPlaybackToken();
  await stopNative(() => {});
  expect(token.signal.aborted).toBe(true);
  expect(token.valid()).toBe(false);
});

test('revocation while the legacy stop is pending prevents a later start', async () => {
  const { legacyPlaybackToken, prepareLegacy, stopNative } = await import(
    '../src/lib/audio/playbackOwnership'
  );
  let release!: () => void;
  let entered!: () => void;
  const ready = new Promise<void>((resolve) => {
    entered = resolve;
  });
  const token = legacyPlaybackToken();
  const preparing = prepareLegacy(token, async () => {
    entered();
    await new Promise<void>((resolve) => {
      release = resolve;
    });
  });
  await ready;
  const claim = stopNative(() => {});
  release();
  await Promise.all([preparing, claim]);
  expect(token.valid()).toBe(false);
});

test('late audition play is stopped after disposal', async () => {
  const player = setup();
  let release!: () => void;
  FakeAudio.start = () =>
    new Promise<void>((resolve) => {
      release = resolve;
    });
  const starting = player.audition('/take', 'Take');
  await Promise.resolve();
  await Promise.resolve();
  player.dispose();
  release();
  await starting;
  expect(FakeAudio.instances[0].paused).toBe(true);
});

test('muted solo tracks do not suppress otherwise audible tracks', async () => {
  const player = setup();
  const doc = {
    ...document,
    tracks: [...document.tracks, { id: 'muted', gain: 1, mute: true, solo: true }],
  } as StudioDocument;
  await player.timeline(doc, assets, () => 'http://local/assets/asset/audio', 0);
  expect(FakeContext.instances[0].sources).toHaveLength(1);
  expect(player.getSnapshot().mode).toBe('timeline');
});

test('long preview starts with one window and requests the next only as playback advances', async () => {
  const player = setup();
  const longAssets = [{ ...assets[0], frames: 240000, duration: 10 }] as StudioAsset[];
  const doc = {
    ...document,
    clips: [{ ...document.clips[0], start_sample: 0, trim_start_sample: 0, trim_end_sample: 0 }],
  } as StudioDocument;
  const urls: string[] = [];
  globalThis.fetch = (async (url) => {
    urls.push(String(url));
    return new Response(new Uint8Array([0]));
  }) as typeof fetch;
  await player.timeline(doc, longAssets, () => 'http://local/assets/asset/audio', 0);
  expect(urls).toHaveLength(1);
  expect(urls[0]).toContain('frames=96000');
  FakeContext.instances[0].currentTime = 0.7;
  await new Promise((resolve) => setTimeout(resolve, 110));
  expect(urls).toHaveLength(2);
  expect(urls[1]).toContain('start_sample=96000');
  expect(FakeContext.instances[0].sources[1].start).toEqual([2.04, 0, 2]);
});
