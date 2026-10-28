import { registerStudioPlaybackStop } from '@/lib/audio/playbackOwnership';
import { clipFrames, timelineEnd } from './domain';
import { AudioMeter } from './meters';
import type { StudioAsset, StudioDocument } from './types';

export interface PlaybackState {
  mode: 'idle' | 'loading' | 'audition' | 'timeline';
  label: string;
  position: number;
  duration: number;
  error: string | null;
}
export class StudioPlayback {
  readonly meters = new AudioMeter();
  private meterInput: AudioNode | null = null;
  private state: PlaybackState = { mode: 'idle', label: '', position: 0, duration: 0, error: null };
  private listeners = new Set<() => void>();
  private audio: HTMLAudioElement | null = null;
  private context: AudioContext | null = null;
  private nodes: AudioBufferSourceNode[] = [];
  private request: AbortController | null = null;
  private epoch = 0;
  private timer: ReturnType<typeof setInterval> | undefined;
  private readonly unregister: () => void;
  constructor(private readonly claim: () => void | Promise<void> = () => {}) {
    this.unregister = registerStudioPlaybackStop(this.stop);
  }
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private emit(patch: Partial<PlaybackState>) {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener();
  }
  stop = () => {
    this.epoch++;
    this.meters.stop();
    this.meterInput = null;
    this.request?.abort();
    this.request = null;
    clearInterval(this.timer);
    if (this.audio) {
      this.audio.onended = null;
      this.audio.ontimeupdate = null;
      this.audio.onerror = null;
      this.audio.onpause = null;
      this.audio.pause();
      this.audio.removeAttribute('src');
      this.audio.load();
      this.audio = null;
    }
    for (const node of this.nodes) {
      try {
        node.stop();
        node.disconnect();
      } catch {
        /* A completed source is already stopped. */
      }
    }
    this.nodes = [];
    if (this.context) void this.context.close().catch(() => {});
    this.context = null;
    this.emit({ mode: 'idle' });
  };
  private async acquire(token: number) {
    try {
      await this.claim();
      return token === this.epoch;
    } catch (error) {
      if (token !== this.epoch) return false;
      this.emit({
        error:
          error instanceof Error
            ? error.message
            : 'The previous playback output could not be stopped.',
      });
      return false;
    }
  }
  async audition(url: string, label: string) {
    this.stop();
    const token = this.epoch;
    if (!(await this.acquire(token))) return;
    const audio = new Audio();
    audio.crossOrigin = 'anonymous';
    audio.src = url;
    this.audio = audio;
    audio.preload = 'metadata';
    audio.onpause = () => {
      if (token === this.epoch) this.stop();
    };
    this.emit({ mode: 'loading', label, position: 0, duration: 0, error: null });
    audio.ontimeupdate = () => {
      if (token === this.epoch)
        this.emit({
          position: audio.currentTime,
          duration: Number.isFinite(audio.duration) ? audio.duration : 0,
        });
    };
    audio.onended = () => {
      if (token === this.epoch) this.stop();
    };
    audio.onerror = () => {
      if (token === this.epoch) {
        this.stop();
        this.emit({ error: 'Audio could not be loaded. Check the server and source file.' });
      }
    };
    try {
      // Unsupported analysis must not prevent ordinary audition playback.
      let routed = false;
      try {
        const context = new AudioContext();
        this.context = context;
        if (typeof context.createMediaElementSource === 'function') {
          const source = context.createMediaElementSource(audio);
          source.connect(context.destination);
          routed = true;
          this.meterInput = this.meters.attach(context);
          if (this.meterInput) source.connect(this.meterInput);
          await context.resume();
          if (token !== this.epoch) return;
        } else {
          void context.close().catch(() => {});
          this.context = null;
          this.meters.unavailable();
        }
      } catch (error) {
        if (routed) throw error;
        if (this.context) void this.context.close().catch(() => {});
        this.context = null;
        this.meters.unavailable();
      }
      if (token !== this.epoch) return;
      await audio.play();
      if (token === this.epoch) this.emit({ mode: 'audition' });
      else {
        audio.pause();
        audio.removeAttribute('src');
        audio.load();
      }
    } catch (error) {
      if (token === this.epoch) {
        this.stop();
        this.emit({ error: error instanceof Error ? error.message : 'Playback failed.' });
      }
    }
  }
  async timeline(
    document: StudioDocument,
    assets: StudioAsset[],
    assetUrl: (id: string) => string,
    fromSeconds: number,
    label = 'Timeline preview',
  ) {
    this.stop();
    const token = this.epoch;
    if (!(await this.acquire(token))) return;
    const controller = new AbortController();
    this.request = controller;
    this.emit({
      mode: 'loading',
      label,
      error: null,
      position: fromSeconds,
      duration: timelineEnd(document, assets) / document.sample_rate,
    });
    try {
      const context = new AudioContext({ sampleRate: document.sample_rate });
      this.context = context;
      await context.resume();
      if (token !== this.epoch) return;
      this.meterInput = this.meters.attach(context);
      const solo = document.tracks.some((track) => track.solo && !track.mute);
      const clips = document.clips.flatMap((clip) => {
        const track = document.tracks.find((t) => t.id === clip.track_id);
        const asset = assets.find((a) => a.id === clip.asset_id);
        if (!track || !asset || track.mute || (solo && !track.solo)) return [];
        const duration = clipFrames(clip, asset, document.sample_rate);
        return clip.start_sample + duration > Math.round(fromSeconds * document.sample_rate)
          ? [{ clip, track, duration }]
          : [];
      });
      if (!clips.length) throw new Error('No audible clips at this playhead.');
      const rate = document.sample_rate;
      const first = Math.round(fromSeconds * rate);
      const last = Math.max(...clips.map(({ clip, duration }) => clip.start_sample + duration));
      const chunk = 2 * rate;
      let origin = 0;
      let cursor = first;
      const loadWindow = async (begin: number, end: number) => {
        const parts = clips.filter(
          ({ clip, duration }) => clip.start_sample < end && clip.start_sample + duration > begin,
        );
        // A finite overlap cap keeps decoded audio and scheduled nodes bounded.
        if (parts.length > 128) throw new Error('Preview has too many overlapping clips.');
        const loaded: Array<{
          part: (typeof parts)[number];
          lo: number;
          hi: number;
          buffer: AudioBuffer;
        }> = [];
        for (const part of parts) {
          const lo = Math.max(begin, part.clip.start_sample);
          const hi = Math.min(end, part.clip.start_sample + part.duration);
          const sourceStart = part.clip.trim_start_sample + lo - part.clip.start_sample;
          const url = new URL(
            assetUrl(part.clip.asset_id),
            globalThis.location?.href ?? 'http://localhost',
          );
          url.pathname = url.pathname.replace(/\/audio$/, '/preview');
          url.searchParams.set('start_sample', String(sourceStart));
          url.searchParams.set('frames', String(hi - lo));
          const response = await fetch(url.toString(), { signal: controller.signal });
          if (!response.ok) throw new Error(`Source audio unavailable (${response.status}).`);
          const buffer = await context.decodeAudioData(await response.arrayBuffer());
          if (token !== this.epoch) return;
          loaded.push({ part, lo, hi, buffer });
        }
        if (!origin) origin = context.currentTime + 0.04;
        if (begin !== first && context.currentTime > origin + (begin - first) / rate)
          throw new Error('Preview underrun. Stop and try again.');
        for (const {
          part: { clip, track, duration },
          lo,
          hi,
          buffer,
        } of loaded) {
          const when = origin + (lo - first) / rate;
          const seconds = (hi - lo) / rate;
          const source = context.createBufferSource();
          source.buffer = buffer;
          const gain = context.createGain();
          const points = Math.max(2, Math.ceil(seconds * 100) + 1);
          const values = Float32Array.from({ length: points }, (_, i) => {
            const pos = lo - clip.start_sample + ((hi - lo) * i) / (points - 1);
            return (
              clip.gain *
              track.gain *
              (clip.fade_in_samples ? Math.min(1, pos / clip.fade_in_samples) : 1) *
              (clip.fade_out_samples ? Math.min(1, (duration - pos) / clip.fade_out_samples) : 1)
            );
          });
          gain.gain.setValueCurveAtTime(values, when, seconds);
          source.connect(gain);
          gain.connect(context.destination);
          if (this.meterInput) gain.connect(this.meterInput);
          source.onended = () => {
            source.disconnect();
            gain.disconnect();
            this.nodes = this.nodes.filter((node) => node !== source);
          };
          source.start(when, 0, seconds);
          this.nodes.push(source);
        }
      };
      await loadWindow(cursor, Math.min(cursor + chunk, last));
      if (token !== this.epoch) return;
      cursor = Math.min(cursor + chunk, last);
      this.emit({ mode: 'timeline' });
      let loading = false;
      this.timer = setInterval(() => {
        if (token !== this.epoch) return;
        this.emit({
          position: Math.min(last / rate, fromSeconds + Math.max(0, context.currentTime - origin)),
        });
        if (context.currentTime >= origin + (last - first) / rate) {
          this.stop();
          return;
        }
        if (
          !loading &&
          cursor < last &&
          context.currentTime >= origin + (cursor - first) / rate - 1.5
        ) {
          loading = true;
          const end = Math.min(cursor + chunk, last);
          void loadWindow(cursor, end)
            .then(() => {
              cursor = end;
              loading = false;
            })
            .catch((error) => {
              if (token !== this.epoch) return;
              this.stop();
              this.emit({
                error: error instanceof Error ? error.message : 'Timeline playback failed.',
              });
            });
        }
      }, 80);
    } catch (error) {
      if (token !== this.epoch) return;
      this.stop();
      this.emit({ error: error instanceof Error ? error.message : 'Timeline playback failed.' });
    }
  }
  dispose() {
    this.unregister();
    this.stop();
    this.listeners.clear();
  }
}
