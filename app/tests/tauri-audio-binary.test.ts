import { expect, mock, test } from 'bun:test';

const calls: unknown[][] = [];
mock.module('@tauri-apps/api/core', () => ({
  invoke: async (...args: unknown[]) => {
    calls.push(args);
    return {};
  },
}));
mock.module('@tauri-apps/api/event', () => ({ listen: async () => () => {} }));
const { tauriAudio } = await import('../../tauri/src/platform/audio');

test('native playback sends the existing byte buffer as a raw IPC body with bounded metadata', async () => {
  const bytes = new Uint8Array([0, 127, 255]);
  await tauriAudio.playToDevices(bytes, ['device-a', 'device-a'], { startTime: 1.5, volume: 0.25 });
  expect(calls[0][0]).toBe('play_audio_to_devices');
  expect(calls[0][1]).toBe(bytes);
  const options = calls[0][2] as { headers: Record<string, string> };
  expect(JSON.parse(options.headers['x-voicebox-playback'])).toEqual({
    deviceIds: ['device-a'],
    options: { startTime: 1.5, volume: 0.25 },
  });
});

test('binary playback routing metadata safely preserves Unicode device identifiers in HTTP headers', async () => {
  await tauriAudio.playToDevices(new Uint8Array([1]), ['device_日本語_🎧'], {
    startTime: 0,
    volume: 1,
  });
  const options = calls.at(-1)![2] as { headers: Record<string, string> };
  const metadata = options.headers['x-voicebox-playback'];
  expect([...metadata].every((character) => character.charCodeAt(0) < 127)).toBe(true);
  expect(JSON.parse(metadata).deviceIds).toEqual(['device_日本語_🎧']);
});

test('incremental preview keeps PCM binary and addresses append/finish to one native session', async () => {
  calls.length = 0;
  const pcm = new Uint8Array([0, 127, 255, 0]);
  await tauriAudio.startPcmStream!(24000, ['selected-device']);
  await tauriAudio.appendPcmStream!('native-7', pcm);
  await tauriAudio.finishPcmStream!('native-7');
  expect(calls[0]).toEqual([
    'start_audio_pcm_stream',
    { sampleRate: 24000, deviceIds: ['selected-device'] },
  ]);
  expect(calls[1][0]).toBe('append_audio_pcm_stream');
  expect(calls[1][1]).toBe(pcm);
  expect(calls[1][2]).toEqual({ headers: { 'x-voicebox-session': 'native-7' } });
  expect(calls[2]).toEqual(['finish_audio_pcm_stream', { sessionId: 'native-7' }]);
});
