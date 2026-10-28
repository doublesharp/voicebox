import { expect, mock, test } from 'bun:test';
import type { StoryItemDetail } from '../src/lib/api/types';
import { StoryAudioCache, storyAudioSource } from '../src/lib/audio/storyAudioCache';

const buffer = () =>
  ({
    duration: 1,
    length: 100,
    numberOfChannels: 1,
    getChannelData: () => new Float32Array(100).fill(0.5),
  }) as unknown as AudioBuffer;
const response = () => ({ ok: true, arrayBuffer: async () => new ArrayBuffer(4) }) as Response;
const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

test('decode concurrency is bounded and pending copies share one request', async () => {
  const finish: ((response: Response) => void)[] = [];
  const fetcher = mock(
    () =>
      new Promise<Response>((resolve) => {
        finish.push(resolve);
      }),
  ) as unknown as typeof fetch;
  const decode = mock(async () => buffer());
  const cache = new StoryAudioCache(decode, { concurrency: 2, fetch: fetcher });
  const first = cache.retain({ key: 'one', url: '/one' });
  const duplicate = cache.retain({ key: 'one', url: '/one' });
  const second = cache.retain({ key: 'two', url: '/two' });
  const third = cache.retain({ key: 'three', url: '/three' });
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(first.promise).toBe(duplicate.promise);
  finish[0](response());
  await first.promise;
  await flush();
  expect(fetcher).toHaveBeenCalledTimes(3);
  finish[1](response());
  finish[2](response());
  await Promise.all([duplicate.promise, second.promise, third.promise]);
  expect(decode).toHaveBeenCalledTimes(3);
  for (const lease of [first, duplicate, second, third]) lease.release();
  cache.clear();
});

test('releasing stale requests prevents decoding even when fetch ignores abort', async () => {
  let finish!: (response: Response) => void;
  const fetcher = mock(
    () =>
      new Promise<Response>((resolve) => {
        finish = resolve;
      }),
  ) as unknown as typeof fetch;
  const decode = mock(async () => buffer());
  const cache = new StoryAudioCache(decode, { fetch: fetcher });
  const lease = cache.retain({ key: 'gone', url: '/gone' });
  lease.release();
  await expect(lease.promise).rejects.toMatchObject({ name: 'AbortError' });
  finish(response());
  await flush();
  expect(decode).not.toHaveBeenCalled();
  expect(cache.get('gone')).toBeUndefined();
});

test('idle full buffers can be evicted while compact peaks remain reusable', async () => {
  const fetcher = mock(async () => response()) as unknown as typeof fetch;
  const cache = new StoryAudioCache(async () => buffer(), { maxBytes: 0, fetch: fetcher });
  const source = { key: 'peaks', url: '/peaks' };
  const lease = cache.retain(source);
  await lease.promise;
  lease.release();
  expect(cache.get(source.key)).toBeUndefined();
  const waveform = cache.retainWaveform(source);
  expect((await waveform.promise).peaks[0][0]).toBe(0.5);
  expect(fetcher).toHaveBeenCalledTimes(1);
  waveform.release();
  cache.clear();
});

test('default version changes and edited file revisions invalidate source identity', () => {
  const item = {
    generation_id: 'generation',
    audio_path: 'old.wav',
    active_version_id: 'version-a',
  } as StoryItemDetail;
  const original = storyAudioSource(item);
  const revised = storyAudioSource({ ...item, audio_path: 'new.wav' });
  const selected = storyAudioSource({ ...item, active_version_id: 'version-b' });
  expect(original.key).not.toBe(revised.key);
  expect(original.key).not.toBe(selected.key);
  expect(revised.url).toContain('revision=new.wav');
});
