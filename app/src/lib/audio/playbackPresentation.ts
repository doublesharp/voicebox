import type WaveSurfer from 'wavesurfer.js';

type OutputContext = Pick<BaseAudioContext, 'currentTime' | 'state'> & {
  baseLatency?: number;
  outputLatency?: number;
};

export function outputLatencySeconds(context: OutputContext): number {
  const valid = (value: number | undefined) =>
    value !== undefined && Number.isFinite(value) && value > 0 ? value : 0;
  return valid(context.baseLatency) + valid(context.outputLatency);
}

/** Map the render clock back to the source position currently reaching output. */
export function createOutputTimeline() {
  let initialPosition = 0;
  const segments: { time: number; position: number; rate: number }[] = [];

  return {
    reset(position = 0) {
      initialPosition = position;
      segments.length = 0;
    },
    record(time: number, position: number, rate: number) {
      segments.push({ time, position, rate });
    },
    read(time: number, latency: number, duration: number): number {
      const outputTime = time - latency;
      let index = segments.length - 1;
      while (index >= 0 && segments[index].time > outputTime) index--;
      const segment = segments[index];
      const position = segment
        ? segment.position + (outputTime - segment.time) * segment.rate
        : (segments[0]?.position ?? initialPosition);
      // Keep the segment spanning outputTime, plus every still-buffered change.
      if (index > 0) segments.splice(0, index);
      return Math.min(duration, Math.max(0, position));
    },
  };
}

/** Presentation only: leave source time, seeking, looping and finish scheduling alone. */
export function bindPlaybackPresentation(
  player: WaveSurfer,
  onTime: (time: number) => void,
  isEnabled: () => boolean,
) {
  const media = player.getMediaElement() as HTMLMediaElement & { getGainNode?: () => GainNode };
  const context = media.getGainNode?.().context as OutputContext | undefined;
  const timeline = createOutputTimeline();
  let frame: number | null = null;
  let disposed = false;

  const cancelDrain = () => {
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
  };
  const update = (sourceTime = player.getCurrentTime()) => {
    if (disposed || !isEnabled()) return;
    const duration = player.getDuration();
    const latency = context ? outputLatencySeconds(context) : 0;
    const time =
      context && latency > 0 && duration > 0
        ? timeline.read(context.currentTime, latency, duration)
        : sourceTime;
    onTime(time);
    if (duration > 0) player.getRenderer().renderProgress(time / duration, player.isPlaying());
  };
  const record = () => {
    if (!context || !isEnabled()) return;
    timeline.record(
      context.currentTime,
      player.getCurrentTime(),
      player.isPlaying() ? player.getPlaybackRate() : 0,
    );
  };
  const subscriptions = [
    player.on('play', () => {
      cancelDrain();
      record();
    }),
    player.on('pause', () => {
      cancelDrain();
      record();
      if (!context) return;
      const stoppedAt = context.currentTime;
      const drain = () => {
        frame = null;
        if (disposed || !isEnabled()) return;
        update();
        // A paused source may still have samples queued in the output device.
        if (
          context.state === 'running' &&
          context.currentTime - outputLatencySeconds(context) < stoppedAt
        ) {
          frame = requestAnimationFrame(drain);
        }
      };
      frame = requestAnimationFrame(drain);
    }),
    player.on('seeking', () => {
      if (player.isPlaying()) record();
      else timeline.reset(player.getCurrentTime());
    }),
    player.on('load', () => {
      cancelDrain();
      timeline.reset();
    }),
  ];
  const destroy = () => {
    if (disposed) return;
    disposed = true;
    cancelDrain();
    subscriptions.forEach((unsubscribe) => {
      unsubscribe();
    });
  };
  subscriptions.push(player.on('destroy', destroy));
  return { update, destroy };
}
