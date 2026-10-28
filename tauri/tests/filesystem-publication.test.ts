import { expect, mock, test } from 'bun:test';

const calls: Array<{ command: string; args: Record<string, unknown> }> = [];
let response: unknown = { status: 'cancelled' };
mock.module('@tauri-apps/api/core', () => ({
  invoke: async (command: string, args: Record<string, unknown>) => {
    calls.push({ command, args });
    return response;
  },
}));
const { tauriFilesystem } = await import('../src/platform/filesystem');

test('native saves preserve cancellation and pass bytes to the chooser-owned command', async () => {
  calls.length = 0;
  response = { status: 'cancelled' };
  expect(await tauriFilesystem.saveFile('a.wav', new Blob(['abc']))).toEqual(response);
  expect(calls).toEqual([
    {
      command: 'publish_file_bytes',
      args: { filename: 'a.wav', filters: [], bytes: [97, 98, 99] },
    },
  ]);
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
