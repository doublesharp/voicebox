import { expect, mock, test } from 'bun:test';

const calls: Array<{ command: string; args: unknown; options?: unknown }> = [];
let response: unknown = { status: 'cancelled' };
mock.module('@tauri-apps/api/core', () => ({
  invoke: async (command: string, args: unknown, options?: unknown) => {
    calls.push(options === undefined ? { command, args } : { command, args, options });
    return response;
  },
}));
const { tauriFilesystem } = await import('../src/platform/filesystem');

test('native saves send raw audio and ASCII-safe metadata while preserving cancellation', async () => {
  calls.length = 0;
  response = { status: 'cancelled' };
  const filters = [{ name: '音声 🎙', extensions: ['wav'] }];
  expect(await tauriFilesystem.saveFile('café 🎙.wav', new Blob(['abc']), filters)).toEqual(response);
  expect(calls).toHaveLength(1);
  expect(calls[0].command).toBe('publish_file_bytes');
  expect(calls[0].args).toBeInstanceOf(Uint8Array);
  expect(Array.from(calls[0].args as Uint8Array)).toEqual([97, 98, 99]);
  const metadata = (calls[0].options as { headers: Record<string, string> }).headers['x-voicebox-export'];
  expect(metadata).toMatch(/^[\x20-\x7e]*$/);
  expect(JSON.parse(metadata)).toEqual({ filename: 'café 🎙.wav', filters });
});

test('large saves retain binary bytes without creating a JSON number array', async () => {
  calls.length = 0;
  const bytes = new Uint8Array(1024 * 1024);
  bytes[0] = 255;
  bytes[bytes.length - 1] = 127;
  await tauriFilesystem.saveFile('long.wav', new Blob([bytes]));
  const body = calls[0].args as Uint8Array;
  expect(body).toBeInstanceOf(Uint8Array);
  expect(body.length).toBe(bytes.length);
  expect(body[0]).toBe(255);
  expect(body[body.length - 1]).toBe(127);
});

test('URL saves pass the URL without fetching the audio in JavaScript', async () => {
  calls.length = 0;
  response = { status: 'saved', path: '/chosen/a.wav' };
  expect(await tauriFilesystem.saveFromUrl!('a.wav', 'http://localhost/audio')).toEqual(response);
  expect(calls).toEqual([
    {
      command: 'publish_file_url',
      args: { filename: 'a.wav', url: 'http://localhost/audio', filters: [] },
    },
  ]);
});
