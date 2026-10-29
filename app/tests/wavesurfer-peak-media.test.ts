import { expect, mock, test } from 'bun:test';
import WaveSurfer from '../node_modules/wavesurfer.js/dist/wavesurfer.js';
import WebAudioPlayer from '../node_modules/wavesurfer.js/dist/webaudio.js';

test('installed WaveSurfer accepts blank URL peaks with real WebAudio duration and seek semantics', async () => {
  const originalFetch = globalThis.fetch;
  const originalBuffer = globalThis.AudioBuffer;
  globalThis.AudioBuffer = class {
    copyFromChannel() {}
    copyToChannel() {}
  } as unknown as typeof AudioBuffer;
  const fetcher = mock(async () => {
    throw new Error('Peaks must never load audio');
  });
  globalThis.fetch = fetcher as unknown as typeof fetch;
  const decode = mock(async () => {
    throw new Error('Peaks must never decode audio');
  });
  const media = new WebAudioPlayer({
    createGain: () => ({ gain: { value: 1 }, connect() {} }),
    destination: {},
    currentTime: 0,
    decodeAudioData: decode,
  } as unknown as AudioContext);
  const events: unknown[] = [];
  const wave = {
    options: { media },
    media,
    decodedData: null,
    isPlaying: () => false,
    emit: (...args: unknown[]) => {
      events.push(args);
    },
    setSrc: (url: string) => {
      media.src = url;
    },
    getDuration: () => media.duration,
    getMediaElement: () => media,
    renderer: { render() {} },
  };
  try {
    const load = (
      WaveSurfer.prototype as unknown as { loadAudio: (...args: unknown[]) => Promise<void> }
    ).loadAudio;
    await load.call(wave, '', undefined, [new Float32Array([0.1, -0.3])], 5);
    expect(media.duration).toBe(5);
    media.currentTime = 2.5;
    expect(media.currentTime).toBe(2.5);
    expect(events).toContainEqual(['ready', 5]);
    expect(fetcher).not.toHaveBeenCalled();
    expect(decode).not.toHaveBeenCalled();
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.AudioBuffer = originalBuffer;
  }
});
