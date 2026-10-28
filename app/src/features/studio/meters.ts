export interface ChannelLevel {
  db: number;
  holdDb: number;
  clipped: boolean;
}
export interface MeterState {
  status: 'idle' | 'active' | 'unavailable' | 'suspended';
  channels: ChannelLevel[];
}
const floor = -60;
export function samplePeak(samples: Float32Array): number {
  let peak = 0;
  for (const value of samples) if (Number.isFinite(value)) peak = Math.max(peak, Math.abs(value));
  return peak ? Math.max(floor, 20 * Math.log10(peak)) : floor;
}
export function heldLevel(db: number, previous: ChannelLevel, elapsed: number): ChannelLevel {
  return {
    db,
    holdDb: Math.max(db, previous.holdDb - Math.max(0, elapsed) * 12),
    clipped: db >= 0 || previous.clipped,
  };
}
const empty = (): ChannelLevel => ({ db: floor, holdDb: floor, clipped: false });

/** Optional analysis tap. Never owns the audible route or a second audio context. */
export class AudioMeter {
  private state: MeterState = { status: 'idle', channels: [empty(), empty()] };
  private listeners = new Set<() => void>();
  private nodes: AudioNode[] = [];
  private timer: ReturnType<typeof setTimeout> | undefined;
  private removeVisibility: (() => void) | undefined;
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private emit(state: MeterState) {
    this.state = state;
    for (const listener of this.listeners) listener();
  }
  unavailable() {
    this.stop();
    this.emit({ ...this.state, status: 'unavailable' });
  }
  attach(context: AudioContext): AudioNode | null {
    this.stop();
    try {
      const input = context.createGain();
      this.nodes.push(input);
      input.channelCount = 2;
      input.channelCountMode = 'explicit'; // Speakers interpretation duplicates mono into L/R.
      const splitter = context.createChannelSplitter(2);
      this.nodes.push(splitter);
      input.connect(splitter);
      const sink = context.createGain();
      this.nodes.push(sink);
      sink.gain.value = 0;
      sink.connect(context.destination);
      const analysers = [0, 1].map((channel) => {
        const analyser = context.createAnalyser();
        this.nodes.push(analyser);
        analyser.fftSize = 8192;
        splitter.connect(analyser, channel);
        analyser.connect(sink);
        return analyser;
      });
      const buffers = analysers.map(() => new Float32Array(8192));
      let last = context.currentTime;
      const heldUntil = [0, 0];
      const tick = () => {
        clearTimeout(this.timer);
        if (typeof document !== 'undefined' && document.hidden) {
          this.emit({ ...this.state, status: 'suspended' });
          return;
        }
        try {
          const now = context.currentTime;
          const channels = analysers.map((analyser, index) => {
            analyser.getFloatTimeDomainData(buffers[index]);
            const db = samplePeak(buffers[index]);
            const previous = this.state.channels[index];
            if (db >= previous.holdDb) heldUntil[index] = now + 1;
            return heldLevel(db, previous, now > heldUntil[index] ? now - last : 0);
          });
          last = now;
          this.emit({ status: 'active', channels });
          this.timer = setTimeout(tick, 50); // 20 Hz, independent of Studio renders.
        } catch {
          this.unavailable();
        }
      };
      if (typeof document !== 'undefined') {
        document.addEventListener('visibilitychange', tick);
        this.removeVisibility = () => document.removeEventListener('visibilitychange', tick);
      }
      tick();
      return input;
    } catch {
      this.unavailable();
      return null;
    }
  }
  stop() {
    clearTimeout(this.timer);
    this.removeVisibility?.();
    this.removeVisibility = undefined;
    for (const node of this.nodes) {
      try {
        node.disconnect();
      } catch {
        /* Already closed. */
      }
    }
    this.nodes = [];
    this.emit({ status: 'idle', channels: [empty(), empty()] });
  }
}
