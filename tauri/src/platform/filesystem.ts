import type { FileFilter, PlatformFilesystem, SaveFileResult } from '@/platform/types';

export const tauriFilesystem: PlatformFilesystem = {
  async saveFile(filename: string, blob: Blob, filters?: FileFilter[]) {
    const { invoke } = await import('@tauri-apps/api/core');
    const bytes = new Uint8Array(await blob.arrayBuffer());
    // Webview header transports require ASCII, including for Unicode names.
    const metadata = JSON.stringify({ filename, filters: filters || [] }).replace(
      /[\u007f-\uffff]/g,
      (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`,
    );
    return invoke<SaveFileResult>('publish_file_bytes', bytes, {
      headers: { 'x-voicebox-export': metadata },
    });
  },

  async saveFromUrl(filename: string, url: string, filters?: FileFilter[]) {
    const { invoke } = await import('@tauri-apps/api/core');
    return invoke<SaveFileResult>('publish_file_url', { filename, url, filters: filters || [] });
  },

  async openPath(path: string) {
    const { open } = await import('@tauri-apps/plugin-shell');
    await open(path);
  },

  async pickDirectory(title: string) {
    const { open } = await import('@tauri-apps/plugin-dialog');
    const selected = await open({ directory: true, title });
    if (!selected) return null;
    const dir = typeof selected === 'string' ? selected : (selected as { path: string }).path;
    return dir || null;
  },
};
