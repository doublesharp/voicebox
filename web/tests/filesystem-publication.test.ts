import { expect, test } from 'bun:test';
import { webFilesystem } from '../src/platform/filesystem';

test('browser save reports a started download, not a disk save', async () => {
  const originalWindow = globalThis.window;
  const originalDocument = globalThis.document;
  let clicked = false;
  const anchor = { href: '', download: '', click: () => { clicked = true; } };
  Object.assign(globalThis, {
    window: { URL: { createObjectURL: () => 'blob:test', revokeObjectURL: () => {} } },
    document: { createElement: () => anchor, body: { appendChild: () => {}, removeChild: () => {} } },
  });
  try {
    expect(await webFilesystem.saveFile('audio.wav', new Blob(['audio']))).toEqual({ status: 'download_started' });
    expect(clicked).toBe(true);
  } finally {
    Object.assign(globalThis, { window: originalWindow, document: originalDocument });
  }
});
