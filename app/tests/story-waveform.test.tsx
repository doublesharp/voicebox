import { afterAll, afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const actualWaveSurfer = await import('wavesurfer.js');
const createWave = mock((_options: unknown) => ({
  load: mock(async (..._args: unknown[]) => {}),
  setOptions: mock((_options: unknown) => {}),
  destroy: mock(() => {}),
}));
mock.module('wavesurfer.js', () => ({ ...actualWaveSurfer, default: { create: createWave } }));
const { StoryClipWaveform } = await import('../src/lib/audio/storyWaveform');
const { storyAudioCache } = await import('../src/lib/audio/storyAudioCache');
const originals = {
  document: globalThis.document,
  style: globalThis.getComputedStyle,
  fetch: globalThis.fetch,
  context: globalThis.AudioContext,
};
let renderer: ReactTestRenderer | undefined;
const props = {
  generationId: 'waveform-source',
  width: 100,
  duration: 1,
  trimStartMs: 0,
  trimEndMs: 0,
};
const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  createWave.mockClear();
  globalThis.document = {
    documentElement: {},
    createElement: () => ({ muted: false, preload: '' }),
  } as unknown as Document;
  globalThis.getComputedStyle = (() => ({
    getPropertyValue: () => '0 0% 100%',
  })) as unknown as typeof getComputedStyle;
  globalThis.fetch = mock(async () =>
    Response.json({ frames: 24000, sample_rate: 24000, channels: 1, peaks: [[-0.2, 0.2]] }),
  ) as unknown as typeof fetch;
  globalThis.AudioContext = class {
    constructor() {
      throw new Error('Waveform display must not decode full audio');
    }
    state = 'running';
    async close() {
      this.state = 'closed';
    }
    async decodeAudioData() {
      return {
        duration: 1,
        length: 24000,
        numberOfChannels: 1,
        getChannelData: () => new Float32Array(24000).fill(0.2),
      };
    }
  } as unknown as typeof AudioContext;
});
afterEach(async () => {
  if (renderer) {
    await act(async () => renderer?.unmount());
    renderer = undefined;
  }
  storyAudioCache.clear();
});
afterAll(() => {
  globalThis.document = originals.document;
  globalThis.getComputedStyle = originals.style;
  globalThis.fetch = originals.fetch;
  globalThis.AudioContext = originals.context;
  mock.module('wavesurfer.js', () => actualWaveSurfer);
});

test('zoom redraws cached peaks without rebuilding or loading the source again', async () => {
  await act(async () => {
    renderer = create(<StoryClipWaveform {...props} />, { createNodeMock: () => ({}) });
    await flush();
  });
  await act(async () => {
    renderer?.update(<StoryClipWaveform {...props} width={200} />);
    await flush();
  });
  expect(createWave).toHaveBeenCalledTimes(1);
  expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  const options = createWave.mock.calls[0][0] as { peaks: Float32Array[]; duration: number };
  expect(options.peaks[0].length).toBeLessThanOrEqual(2048);
  expect(options.duration).toBe(1);
  expect(createWave.mock.results[0].value.load).not.toHaveBeenCalled();
});

test('split waveforms share the same pending peak envelope', async () => {
  await act(async () => {
    renderer = create(
      <>
        <StoryClipWaveform {...props} />
        <StoryClipWaveform {...props} trimStartMs={100} />
      </>,
      { createNodeMock: () => ({}) },
    );
    await flush();
  });
  expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  expect(createWave).toHaveBeenCalledTimes(2);
});
