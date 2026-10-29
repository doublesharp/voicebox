import { afterEach, expect, mock, test } from 'bun:test';
import {
  convertToWav,
  createAudioUrl,
  downloadAudio,
  formatAudioDuration,
  getAudioDuration,
} from '../src/lib/utils/audio';

const original = {
  context: globalThis.AudioContext,
  audio: globalThis.Audio,
  document: globalThis.document,
  createUrl: URL.createObjectURL,
  revokeUrl: URL.revokeObjectURL,
};
afterEach(() => {
  globalThis.AudioContext = original.context;
  globalThis.Audio = original.audio;
  globalThis.document = original.document;
  URL.createObjectURL = original.createUrl;
  URL.revokeObjectURL = original.revokeUrl;
});

function context(result: AudioBuffer | Error) {
  const close = mock(async () => {});
  const decode = mock(async (_data: ArrayBuffer) => {
    if (result instanceof Error) throw result;
    return result;
  });
  globalThis.AudioContext = class {
    decodeAudioData = decode;
    close = close;
  } as unknown as typeof AudioContext;
  return { close, decode };
}

function buffer(channels: number[][], sampleRate = 8000): AudioBuffer {
  return {
    numberOfChannels: channels.length,
    length: channels[0].length,
    sampleRate,
    duration: channels[0].length / sampleRate,
    getChannelData: (channel: number) => Float32Array.from(channels[channel]),
  } as AudioBuffer;
}

test('WAV encoding preserves stereo interleaving, PCM limits and the file header', async () => {
  const { close, decode } = context(
    buffer([
      [-2, -0.5, 0, 2],
      [1, 0.5, -1, 0],
    ]),
  );
  const input = new Blob(['encoded input']);
  const wav = await convertToWav(input);
  const bytes = await wav.arrayBuffer();
  const view = new DataView(bytes);
  expect(wav.type).toBe('audio/wav');
  expect(new TextDecoder().decode(bytes.slice(0, 4))).toBe('RIFF');
  expect(new TextDecoder().decode(bytes.slice(8, 12))).toBe('WAVE');
  expect(view.getUint32(4, true)).toBe(bytes.byteLength - 8);
  expect(view.getUint16(20, true)).toBe(1);
  expect(view.getUint16(22, true)).toBe(2);
  expect(view.getUint32(24, true)).toBe(8000);
  expect(view.getUint32(28, true)).toBe(32000);
  expect(view.getUint16(32, true)).toBe(4);
  expect(view.getUint16(34, true)).toBe(16);
  expect(view.getUint32(40, true)).toBe(16);
  expect(Array.from({ length: 8 }, (_, i) => view.getInt16(44 + i * 2, true))).toEqual([
    -32768, 32767, -16384, 16383, 0, -32768, 32767, 0,
  ]);
  expect(decode).toHaveBeenCalledWith(await input.arrayBuffer());
  expect(close).toHaveBeenCalledTimes(1);
});

test.each([
  'read',
  'decode',
] as const)('failed %s still releases the audio context', async (stage) => {
  const failure = new Error(`${stage} failed`);
  const { close } = context(failure);
  const input =
    stage === 'read'
      ? ({
          arrayBuffer: async () => {
            throw failure;
          },
        } as Blob)
      : new Blob(['invalid audio']);
  await expect(convertToWav(input)).rejects.toBe(failure);
  expect(close).toHaveBeenCalledTimes(1);
});

test('recorded duration bypasses decoding, while nonfinite metadata uses decoded duration', async () => {
  const { close, decode } = context(buffer([[0, 0]], 2));
  const file = Object.assign(new File(['audio'], 'reference.wav'), { recordedDuration: 3.5 });
  expect(await getAudioDuration(file)).toBe(3.5);
  expect(decode).not.toHaveBeenCalled();
  file.recordedDuration = Number.POSITIVE_INFINITY;
  expect(await getAudioDuration(file)).toBe(1);
  expect(close).toHaveBeenCalledTimes(1);
});

test.each([
  12.5,
  Number.NaN,
  'error',
] as const)('metadata fallback %s revokes its object URL and closes the failed decoder', async (duration) => {
  const { close } = context(new Error('unsupported format'));
  const listeners = new Map<string, () => void>();
  const revoke = mock(() => {});
  URL.createObjectURL = () => 'blob:duration-fixture';
  URL.revokeObjectURL = revoke;
  globalThis.Audio = class {
    duration = duration;
    addEventListener(name: string, callback: () => void) {
      listeners.set(name, callback);
    }
    set src(value: string) {
      expect(value).toBe('blob:duration-fixture');
      queueMicrotask(() => listeners.get(duration === 'error' ? 'error' : 'loadedmetadata')?.());
    }
  } as unknown as typeof Audio;
  const result = getAudioDuration(new File(['audio'], 'reference.wav'));
  if (duration === 'error') await expect(result).rejects.toThrow('Failed to load audio file');
  else if (Number.isNaN(duration)) await expect(result).rejects.toThrow('invalid duration');
  else expect(await result).toBe(duration);
  expect(revoke).toHaveBeenCalledWith('blob:duration-fixture');
  expect(close).toHaveBeenCalledTimes(1);
});

test('download creates, activates and removes the link; duration formatting is stable', () => {
  const link = { href: '', download: '', click: mock(() => {}) };
  const append = mock(() => {});
  const remove = mock(() => {});
  globalThis.document = {
    createElement: (tag: string) => {
      expect(tag).toBe('a');
      return link;
    },
    body: { appendChild: append, removeChild: remove },
  } as unknown as Document;
  downloadAudio('/audio/a', 'take.wav');
  expect(link.href).toBe('/audio/a');
  expect(link.download).toBe('take.wav');
  expect(append).toHaveBeenCalledWith(link);
  expect(link.click).toHaveBeenCalledTimes(1);
  expect(remove).toHaveBeenCalledWith(link);
  expect(createAudioUrl('take', 'http://localhost:17493')).toBe(
    'http://localhost:17493/audio/take',
  );
  expect([0, 9.9, 60, 125].map(formatAudioDuration)).toEqual(['0:00', '0:09', '1:00', '2:05']);
});
