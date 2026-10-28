import { describe, expect, mock, test } from 'bun:test';
import type WaveSurfer from 'wavesurfer.js';
import {
  bindPlaybackPresentation,
  createOutputTimeline,
  outputLatencySeconds,
} from '../src/lib/audio/playbackPresentation';

describe('audible presentation timeline', () => {
  test('keeps the playhead at the start until the first samples reach output', () => {
    const timeline = createOutputTimeline();
    timeline.record(10, 0, 1);
    expect(timeline.read(10.1, 0.15, 5)).toBe(0);
    expect(timeline.read(11, 0.15, 5)).toBeCloseTo(0.85);
  });

  test('uses reported route latency on each read, including a low latency route', () => {
    const timeline = createOutputTimeline();
    timeline.record(0, 0, 1);
    expect(timeline.read(1, 0.15, 5)).toBeCloseTo(0.85);
    expect(timeline.read(1.1, 0.01, 5)).toBeCloseTo(1.09);
    expect(timeline.read(1.2, 0.3, 5)).toBeCloseTo(0.9);
  });

  test('finishes buffered samples after pause, then holds the source position', () => {
    const timeline = createOutputTimeline();
    timeline.record(0, 0, 1);
    timeline.record(1, 1, 0);
    expect(timeline.read(1.05, 0.15, 5)).toBeCloseTo(0.9);
    expect(timeline.read(1.2, 0.15, 5)).toBe(1);
    expect(timeline.read(10, 0.15, 5)).toBe(1);
  });

  test('presents queued seek and loop changes only when their samples reach output', () => {
    const timeline = createOutputTimeline();
    timeline.record(0, 0, 1);
    timeline.record(1, 4, 1);
    expect(timeline.read(1.1, 0.15, 5)).toBeCloseTo(0.95);
    expect(timeline.read(1.2, 0.15, 5)).toBeCloseTo(4.05);
    timeline.record(2, 0, 1);
    expect(timeline.read(2.1, 0.15, 5)).toBeCloseTo(4.95);
    expect(timeline.read(2.2, 0.15, 5)).toBeCloseTo(0.05);
  });

  test('retains pause/resume transitions that have not reached output yet', () => {
    const timeline = createOutputTimeline();
    timeline.record(0, 0, 1);
    timeline.record(1, 1, 0);
    timeline.record(1.05, 1, 1);
    expect(timeline.read(1.1, 0.15, 5)).toBeCloseTo(0.95);
    expect(timeline.read(1.18, 0.15, 5)).toBe(1);
    expect(timeline.read(1.25, 0.15, 5)).toBeCloseTo(1.05);
  });

  test('scales source position by rate and resets history for a new clip or paused seek', () => {
    const timeline = createOutputTimeline();
    timeline.record(0, 0, 2);
    expect(timeline.read(1, 0.15, 5)).toBeCloseTo(1.7);
    timeline.reset(3);
    expect(timeline.read(1, 0.15, 5)).toBe(3);
    timeline.reset();
    expect(timeline.read(1, 0.15, 5)).toBe(0);
    timeline.record(1, 0, 1);
    expect(timeline.read(100, 0, 5)).toBe(5);
  });

  test('has no invented delay when APIs are missing and rejects invalid values', () => {
    const context = { currentTime: 1, state: 'running' as const };
    expect(outputLatencySeconds(context)).toBe(0);
    expect(
      outputLatencySeconds({ ...context, baseLatency: 0.003, outputLatency: 0.15 }),
    ).toBeCloseTo(0.153);
    expect(outputLatencySeconds({ ...context, baseLatency: NaN, outputLatency: -1 })).toBe(0);
    expect(outputLatencySeconds({ ...context, outputLatency: Infinity })).toBe(0);
  });
});

function fixture() {
  const context = { currentTime: 0, state: 'running', outputLatency: 0.15 };
  let sourceTime = 0;
  let playing = false;
  let enabled = true;
  const listeners = new Map<string, Set<() => void>>();
  const emit = (event: string) => {
    listeners.get(event)?.forEach((callback) => {
      callback();
    });
  };
  const renderProgress = mock((_progress: number, _isPlaying?: boolean) => {});
  const onTime = mock((_time: number) => {});
  const player = {
    getCurrentTime: () => sourceTime,
    getDuration: () => 5,
    getPlaybackRate: () => 1,
    isPlaying: () => playing,
    getMediaElement: () => ({ getGainNode: () => ({ context }) }),
    getRenderer: () => ({ renderProgress }),
    on(event: string, callback: () => void) {
      const handlers = listeners.get(event) ?? new Set<() => void>();
      handlers.add(callback);
      listeners.set(event, handlers);
      return () => {
        handlers.delete(callback);
      };
    },
  };
  const frames = new Map<number, FrameRequestCallback>();
  let sequence = 0;
  const originalRequest = globalThis.requestAnimationFrame;
  const originalCancel = globalThis.cancelAnimationFrame;
  globalThis.requestAnimationFrame = (callback) => {
    frames.set(++sequence, callback);
    return sequence;
  };
  globalThis.cancelAnimationFrame = (id) => {
    frames.delete(id);
  };
  const binding = bindPlaybackPresentation(player as unknown as WaveSurfer, onTime, () => enabled);
  return {
    binding,
    context,
    emit,
    onTime,
    renderProgress,
    frames,
    source(time: number, isPlaying: boolean) {
      sourceTime = time;
      playing = isPlaying;
    },
    disable() {
      enabled = false;
    },
    rawTime: player.getCurrentTime,
    frame() {
      const callbacks = [...frames.values()];
      frames.clear();
      callbacks.forEach((callback) => {
        callback(0);
      });
    },
    cleanup() {
      binding.destroy();
      globalThis.requestAnimationFrame = originalRequest;
      globalThis.cancelAnimationFrame = originalCancel;
    },
  };
}

test('corrects both display callbacks and waveform without changing source time', () => {
  const f = fixture();
  try {
    f.source(0, true);
    f.emit('play');
    f.context.currentTime = 1;
    f.source(1, true);
    f.binding.update(1);
    expect(f.onTime).toHaveBeenLastCalledWith(0.85);
    expect(f.renderProgress.mock.lastCall?.[0]).toBeCloseTo(0.17);
    expect(f.renderProgress.mock.lastCall?.[1]).toBe(true);
    expect(f.rawTime()).toBe(1);
    f.source(1, false);
    f.emit('pause');
    f.context.currentTime = 1.1;
    f.frame();
    expect(f.onTime.mock.lastCall?.[0]).toBeCloseTo(0.95);
    f.context.currentTime = 1.2;
    f.frame();
    expect(f.onTime).toHaveBeenLastCalledWith(1);
    expect(f.frames.size).toBe(0);
  } finally {
    f.cleanup();
  }
});

test('does not overwrite native-owned time and releases pending animation on destroy/load', () => {
  const f = fixture();
  try {
    f.source(0, true);
    f.emit('play');
    f.context.currentTime = 1;
    f.source(1, false);
    f.emit('pause');
    expect(f.frames.size).toBe(1);
    f.disable();
    f.frame();
    f.binding.update(1);
    expect(f.onTime).not.toHaveBeenCalled();
    expect(f.renderProgress).not.toHaveBeenCalled();
    expect(f.frames.size).toBe(0);
    f.emit('pause');
    f.emit('load');
    expect(f.frames.size).toBe(0);
    f.emit('destroy');
    f.binding.update(1);
    expect(f.onTime).not.toHaveBeenCalled();
  } finally {
    f.cleanup();
  }
});
