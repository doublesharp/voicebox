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

test('a cold waveform fetches compact peaks without fetching or decoding playback audio', async () => {
  const fetcher = mock(async (url: string) => {
    expect(url).toContain('/audio/generation/waveform?');
    expect(url).toContain('revision=take-a');
    return Response.json({
      frames: 24000,
      sample_rate: 24000,
      channels: 1,
      peaks: [
        [-0.25, 0.5],
        [-0.75, 0.1],
      ],
    });
  }) as unknown as typeof fetch;
  const decode = mock(async () => buffer());
  const cache = new StoryAudioCache(decode, { fetch: fetcher });
  const source = { key: 'revision', url: '/audio/generation?revision=take-a' };
  const first = cache.retainWaveform(source);
  const second = cache.retainWaveform(source);
  const result = await first.promise;
  expect(result.duration).toBe(1);
  expect([...result.peaks[0]]).toEqual([0.5, -0.75]);
  await second.promise;
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(decode).not.toHaveBeenCalled();
  expect(cache.get(source.key)).toBeUndefined();
  first.release();
  second.release();
  cache.clear();
});

test('waveform requests have a finite concurrency budget and cancelled queued sources never start', async () => {
  const finish: Array<(response: Response) => void> = [];
  const fetcher = mock(
    () =>
      new Promise<Response>((resolve) => {
        finish.push(resolve);
      }),
  ) as unknown as typeof fetch;
  const cache = new StoryAudioCache(
    async () => {
      throw new Error('No audio decode expected');
    },
    { concurrency: 2, fetch: fetcher },
  );
  const leases = [1, 2, 3].map((id) =>
    cache.retainWaveform({ key: String(id), url: `/audio/${id}` }),
  );
  expect(fetcher).toHaveBeenCalledTimes(2);
  leases[2].release();
  await expect(leases[2].promise).rejects.toMatchObject({ name: 'AbortError' });
  for (const resolve of finish)
    resolve(Response.json({ frames: 10, sample_rate: 10, channels: 1, peaks: [[0, 0.5]] }));
  await Promise.all(leases.slice(0, 2).map((lease) => lease.promise));
  await flush();
  expect(fetcher).toHaveBeenCalledTimes(2);
  for (const lease of leases) lease.release();
  cache.clear();
});

test('shared peak requests abort only after their final waveform consumer leaves', async () => {
  let complete!: (response: Response) => void;
  let signal!: AbortSignal;
  const fetcher = mock((_url: string, options: RequestInit) => {
    signal = options.signal!;
    return new Promise<Response>((resolve) => {
      complete = resolve;
    });
  }) as unknown as typeof fetch;
  const cache = new StoryAudioCache(async () => buffer(), { fetch: fetcher });
  const source = { key: 'shared', url: '/audio/shared' };
  const a = cache.retainWaveform(source),
    b = cache.retainWaveform(source);
  a.release();
  expect(signal.aborted).toBe(false);
  b.release();
  expect(signal.aborted).toBe(true);
  await expect(a.promise).rejects.toMatchObject({ name: 'AbortError' });
  complete(Response.json({ frames: 10, sample_rate: 10, channels: 1, peaks: [[0, 0.5]] }));
  await flush();
  cache.clear();
});
