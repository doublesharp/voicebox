import { apiClient } from '../api/client';
import type { StoryItemDetail } from '../api/types';

export interface StoryAudioSource {
  key: string;
  url: string;
}

export interface StoryWaveform {
  peaks: Float32Array[];
  duration: number;
}

export interface StoryAudioAsset extends StoryWaveform {
  buffer: AudioBuffer;
}

export interface StoryAudioLease<T> {
  promise: Promise<T>;
  release: () => void;
}

/** Include the selected file revision, even for an unpinned generation default. */
export function storyAudioSource(
  item: Pick<
    StoryItemDetail,
    'generation_id' | 'version_id' | 'active_version_id' | 'audio_path' | 'versions'
  >,
): StoryAudioSource {
  const versionId =
    item.version_id ||
    item.active_version_id ||
    item.versions?.find((version) => version.is_default)?.id;
  const version = item.versions?.find((candidate) => candidate.id === versionId);
  const revision = version?.audio_path || item.audio_path || '';
  const url = versionId
    ? apiClient.getVersionAudioUrl(versionId, revision)
    : `${apiClient.getAudioUrl(item.generation_id)}${revision ? `?revision=${encodeURIComponent(revision)}` : ''}`;
  return { key: url, url };
}

let contextHolder: { context: AudioContext; users: number } | null = null;

/** Decode and playback share a context, closed once all its consumers leave. */
export function retainStoryAudioContext() {
  if (!contextHolder || contextHolder.context.state === 'closed') {
    contextHolder = { context: new AudioContext(), users: 0 };
  }
  const holder = contextHolder;
  holder.users++;
  let released = false;
  return {
    context: holder.context,
    release: () => {
      if (released) return;
      released = true;
      holder.users--;
      if (holder.users === 0) {
        if (contextHolder === holder) contextHolder = null;
        if (holder.context.state !== 'closed') void holder.context.close().catch(() => {});
      }
    },
  };
}

function waveformFor(buffer: AudioBuffer): StoryWaveform {
  const points = Math.min(2048, buffer.length);
  const peaks = Array.from({ length: buffer.numberOfChannels }, (_, channel) => {
    const samples = buffer.getChannelData(channel);
    const result = new Float32Array(points);
    for (let point = 0; point < points; point++) {
      const start = Math.floor((point * samples.length) / points);
      const end = Math.floor(((point + 1) * samples.length) / points);
      let peak = 0;
      for (let sample = start; sample < end; sample++) {
        if (Math.abs(samples[sample]) > Math.abs(peak)) peak = samples[sample];
      }
      result[point] = peak;
    }
    return result;
  });
  return { peaks, duration: buffer.duration };
}

interface CacheEntry {
  source: StoryAudioSource;
  promise: Promise<StoryAudioAsset>;
  resolve: (asset: StoryAudioAsset) => void;
  reject: (error: unknown) => void;
  controller: AbortController;
  users: Set<symbol>;
  asset?: StoryAudioAsset;
  used: number;
}

const aborted = () => new DOMException('Story audio request cancelled', 'AbortError');

/**
 * Deduplicate both pending and completed decodes. The budget applies to idle
 * buffers; buffers still needed by playback may exceed it for a long source.
 */
export class StoryAudioCache {
  private entries = new Map<string, CacheEntry>();
  private waveforms = new Map<string, StoryWaveform>();
  private queue: CacheEntry[] = [];
  private running = 0;
  private sequence = 0;

  constructor(
    private decode: (data: ArrayBuffer) => Promise<AudioBuffer>,
    private options: { concurrency?: number; maxBytes?: number; fetch?: typeof fetch } = {},
  ) {}

  retain(source: StoryAudioSource): StoryAudioLease<StoryAudioAsset> {
    let entry = this.entries.get(source.key);
    if (!entry) {
      let resolve!: CacheEntry['resolve'];
      let reject!: CacheEntry['reject'];
      const promise = new Promise<StoryAudioAsset>((success, failure) => {
        resolve = success;
        reject = failure;
      });
      entry = {
        source,
        promise,
        resolve,
        reject,
        controller: new AbortController(),
        users: new Set(),
        used: ++this.sequence,
      };
      entry.controller.signal.addEventListener('abort', () => reject(aborted()), { once: true });
      // A released queued request may have no remaining promise subscribers.
      void promise.catch(() => {});
      this.entries.set(source.key, entry);
      this.queue.push(entry);
    }
    const token = Symbol();
    entry.users.add(token);
    entry.used = ++this.sequence;
    const promise = entry.promise;
    this.pump();
    return { promise, release: () => this.release(source.key, token) };
  }

  retainWaveform(source: StoryAudioSource): StoryAudioLease<StoryWaveform> {
    const waveform = this.waveforms.get(source.key);
    if (waveform) {
      this.waveforms.delete(source.key);
      this.waveforms.set(source.key, waveform);
      return { promise: Promise.resolve(waveform), release: () => {} };
    }
    const { promise, release } = this.retain(source);
    return {
      promise: promise.then(({ peaks, duration }) => ({ peaks, duration })).finally(release),
      release,
    };
  }

  get(key: string): StoryAudioAsset | undefined {
    const entry = this.entries.get(key);
    if (entry) entry.used = ++this.sequence;
    return entry?.asset;
  }

  prune(keep: ReadonlySet<string>) {
    for (const [key, entry] of this.entries) {
      if (!keep.has(key) && entry.users.size === 0) this.remove(key, entry);
    }
    for (const key of this.waveforms.keys()) if (!keep.has(key)) this.waveforms.delete(key);
  }

  clear() {
    for (const [key, entry] of this.entries) this.remove(key, entry);
    this.waveforms.clear();
  }

  private release(key: string, token: symbol) {
    const entry = this.entries.get(key);
    if (!entry || !entry.users.delete(token)) return;
    if (entry.users.size === 0 && !entry.asset) this.remove(key, entry);
    this.evict();
  }

  private remove(key: string, entry: CacheEntry) {
    if (this.entries.get(key) !== entry) return;
    this.entries.delete(key);
    if (!entry.asset) entry.controller.abort();
  }

  private pump() {
    const concurrency = Math.max(1, this.options.concurrency ?? 3);
    while (this.running < concurrency && this.queue.length) {
      const entry = this.queue.shift()!;
      if (entry.controller.signal.aborted) continue;
      this.running++;
      void this.load(entry).finally(() => {
        this.running--;
        this.pump();
      });
    }
  }

  private async load(entry: CacheEntry) {
    const { signal } = entry.controller;
    try {
      const response = await (this.options.fetch ?? globalThis.fetch)(entry.source.url, { signal });
      if (!response.ok) throw new Error(`Story audio failed to load (${response.status})`);
      const bytes = await response.arrayBuffer();
      if (signal.aborted) throw aborted();
      const buffer = await this.decode(bytes);
      if (signal.aborted || this.entries.get(entry.source.key) !== entry) throw aborted();
      if (buffer.length === 0 || !Number.isFinite(buffer.duration) || buffer.duration <= 0) {
        throw new Error('Story audio contains no playable samples');
      }
      const waveform = waveformFor(buffer);
      entry.asset = { buffer, ...waveform };
      this.waveforms.set(entry.source.key, waveform);
      while (this.waveforms.size > 256) this.waveforms.delete(this.waveforms.keys().next().value!);
      entry.resolve(entry.asset);
      this.evict();
    } catch (error) {
      if (this.entries.get(entry.source.key) === entry) this.entries.delete(entry.source.key);
      entry.reject(error);
    }
  }

  private evict() {
    let bytes = 0;
    for (const entry of this.entries.values()) {
      if (entry.asset) bytes += entry.asset.buffer.length * entry.asset.buffer.numberOfChannels * 4;
    }
    const candidates = [...this.entries.entries()]
      .filter(([, entry]) => entry.asset && entry.users.size === 0)
      .sort((a, b) => a[1].used - b[1].used);
    for (const [key, entry] of candidates) {
      if (bytes <= (this.options.maxBytes ?? 128 * 1024 * 1024) && this.entries.size <= 64) break;
      bytes -= entry.asset!.buffer.length * entry.asset!.buffer.numberOfChannels * 4;
      this.entries.delete(key);
    }
  }
}

export const storyAudioCache = new StoryAudioCache(async (bytes) => {
  const { context, release } = retainStoryAudioContext();
  try {
    if (context.state === 'closed') throw new Error('Story audio context is closed');
    return await context.decodeAudioData(bytes);
  } finally {
    release();
  }
});
