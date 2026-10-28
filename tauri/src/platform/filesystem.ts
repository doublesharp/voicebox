import type { FileFilter, PlatformFilesystem, SaveFileResult } from '@/platform/types';

export const tauriFilesystem: PlatformFilesystem = {
  async saveFile(filename: string, blob: Blob, filters?: FileFilter[]) {
    const { invoke } = await import('@tauri-apps/api/core');
    const bytes = Array.from(new Uint8Array(await blob.arrayBuffer()));
    return invoke<SaveFileResult>('publish_file_bytes', {
      filename,
      filters: filters || [],
      bytes,
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
