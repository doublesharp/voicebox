import { expect, mock, test } from 'bun:test';
import {
  NativePlaybackController,
  type NativePlaybackUpdate,
} from '../src/lib/audio/nativePlayback';
import type {
  NativePlaybackEvent,
  NativePlaybackSession,
  PlatformAudio,
} from '../src/platform/types';

function harness() {
  let emit: (event: NativePlaybackEvent) => void = () => {};
  const updates: NativePlaybackUpdate[] = [];
  const unlisten = mock(() => {});
  const stopPlayback = mock(async (_id?: string) => {});
  const controlPlayback = mock(async (_id: string, _control: unknown) => {});
  const playToDevices = mock(async (_data: Uint8Array, _ids: string[], _options: unknown) => ({
    session_id: 'session-1',
    duration: 5,
    position: 0,
  }));
  const subscribeToPlayback = mock(async (callback: typeof emit) => {
    emit = callback;
    return unlisten;
  });
  const audio = {
    playToDevices,
    stopPlayback,
    controlPlayback,
    subscribeToPlayback,
  } as unknown as PlatformAudio;
  const fetchAudio = mock(async () => new Response(new Uint8Array(4)));
  const controller = new NativePlaybackController(
    audio,
    (update) => updates.push(update),
    fetchAudio,
  );
  return {
    controller,
    fetchAudio,
    updates,
    emit: (event: NativePlaybackEvent) => emit(event),
    playToDevices,
    stopPlayback,
    controlPlayback,
    subscribeToPlayback,
    unlisten,
  };
}

test('routes pause, seek and zero volume to the owned session and ignores stale events', async () => {
  const h = harness();
  await h.controller.start('/audio', ['one', 'one', 'two'], 1, 0.5);
  expect(h.playToDevices.mock.calls[0][1]).toEqual(['one', 'two']);
  expect(h.playToDevices.mock.calls[0][2]).toEqual({ startTime: 1, volume: 0.5 });
  await h.controller.control({ paused: true, position: 2, volume: 0 });
  expect(h.controlPlayback).toHaveBeenCalledWith('session-1', {
    paused: true,
    position: 2,
    volume: 0,
  });
  h.emit({ session_id: 'old-session', state: 'ended', position: 5, duration: 5 });
  expect(h.controller.hasSession).toBe(true);
  h.emit({ session_id: 'session-1', state: 'paused', position: 2, duration: 5 });
  expect(h.updates.at(-1)?.state).toBe('paused');
  await h.controller.stop();
  expect(h.stopPlayback).toHaveBeenCalledWith('session-1');
  h.controller.dispose();
  expect(h.unlisten).toHaveBeenCalledTimes(1);
});

test('retains an end event delivered before the start acknowledgement', async () => {
  const h = harness();
  h.playToDevices.mockImplementation(async () => {
    h.emit({ session_id: 'short', state: 'ended', position: 0.01, duration: 0.01 });
    return { session_id: 'short', duration: 0.01, position: 0 };
  });
  await h.controller.start('/audio', ['one'], 0, 1);
  expect(h.updates.at(-1)?.state).toBe('ended');
  expect(h.controller.hasSession).toBe(false);
  h.controller.dispose();
});

test('cancels a pending fetch before it can start native audio', async () => {
  const h = harness();
  let resolveFetch!: (value: Response) => void;
  let fetchStarted!: () => void;
  const started = new Promise<void>((resolve) => {
    fetchStarted = resolve;
  });
  h.fetchAudio.mockImplementation(
    () =>
      new Promise((resolve) => {
        resolveFetch = resolve;
        fetchStarted();
      }),
  );
  const pending = h.controller.start('/slow-audio', ['one'], 0, 1);
  await started;
  await h.controller.stop();
  resolveFetch(new Response(new Uint8Array(4)));
  await pending;
  expect(h.playToDevices).not.toHaveBeenCalled();
  h.controller.dispose();
});

test('dispose stops a native session that acknowledges late', async () => {
  const h = harness();
  let resolveStart!: (value: NativePlaybackSession) => void;
  let nativeStarted!: () => void;
  const started = new Promise<void>((resolve) => {
    nativeStarted = resolve;
  });
  h.playToDevices.mockImplementation(
    () =>
      new Promise((resolve) => {
        resolveStart = resolve;
        nativeStarted();
      }),
  );
  const pending = h.controller.start('/audio', ['one'], 0, 1);
  await started;
  h.controller.dispose();
  resolveStart({ session_id: 'late', duration: 5, position: 0 });
  await pending;
  expect(h.stopPlayback).toHaveBeenCalledWith('late');
  expect(h.updates.map((event) => event.state)).toEqual(['starting']);
});

test('reports a failed selected output without falling back to default playback', async () => {
  const h = harness();
  h.playToDevices.mockImplementation(async () => {
    throw new Error('Selected output unavailable');
  });
  await h.controller.start('/audio', ['missing'], 0, 1);
  expect(h.updates.at(-1)?.state).toBe('error');
  expect(h.updates.at(-1)?.error).toContain('Selected output unavailable');
  expect(h.controller.hasSession).toBe(false);
  h.controller.dispose();
});

test('applies volume and seek changes made while native startup is pending', async () => {
  const h = harness();
  let resolveStart!: (value: NativePlaybackSession) => void;
  let nativeStarted!: () => void;
  const started = new Promise<void>((resolve) => {
    nativeStarted = resolve;
  });
  h.playToDevices.mockImplementation(
    () =>
      new Promise((resolve) => {
        resolveStart = resolve;
        nativeStarted();
      }),
  );
  const pending = h.controller.start('/audio', ['one'], 0, 1);
  await started;
  await h.controller.control({ position: 2, volume: 0, paused: true });
  resolveStart({ session_id: 'pending-controls', duration: 5, position: 0 });
  await pending;
  expect(h.controlPlayback).toHaveBeenCalledWith('pending-controls', {
    position: 2,
    volume: 0,
    paused: true,
  });
  expect(h.updates.at(-1)?.position).toBe(2);
  expect(h.updates.at(-1)?.state).toBe('paused');
  h.controller.dispose();
});
