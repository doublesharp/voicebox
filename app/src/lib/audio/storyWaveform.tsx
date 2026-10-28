import { useEffect, useRef } from 'react';
import WaveSurfer from 'wavesurfer.js';
import type { GenerationVersionResponse } from '../api/types';
import { storyAudioCache, storyAudioSource } from './storyAudioCache';

export function StoryClipWaveform({
  generationId,
  versionId,
  activeVersionId,
  audioRevision = '',
  versions,
  width,
  trimStartMs,
  trimEndMs,
  duration,
}: {
  generationId: string;
  versionId?: string;
  activeVersionId?: string;
  audioRevision?: string;
  versions?: GenerationVersionResponse[];
  width: number;
  trimStartMs: number;
  trimEndMs: number;
  duration: number;
}) {
  const waveformRef = useRef<HTMLDivElement>(null);
  const wavesurferRef = useRef<WaveSurfer | null>(null);
  const effectiveDurationMs = duration * 1000 - trimStartMs - trimEndMs;
  const fullWaveformWidth =
    effectiveDurationMs > 0 ? (width / effectiveDurationMs) * duration * 1000 : width;
  const offsetX =
    effectiveDurationMs > 0 ? (trimStartMs / (duration * 1000)) * fullWaveformWidth : 0;
  const widthRef = useRef(fullWaveformWidth);
  widthRef.current = fullWaveformWidth;
  const { key: sourceKey, url: sourceUrl } = storyAudioSource({
    generation_id: generationId,
    version_id: versionId,
    active_version_id: activeVersionId,
    audio_path: audioRevision,
    versions,
  });

  useEffect(() => {
    let disposed = false;
    const { promise, release } = storyAudioCache.retainWaveform({ key: sourceKey, url: sourceUrl });
    void promise
      .then(({ peaks, duration: audioDuration }) => {
        if (disposed || !waveformRef.current) return;
        const color = getComputedStyle(document.documentElement)
          .getPropertyValue('--accent-foreground')
          .trim();
        const waveColor = color ? `hsl(${color})` : '';
        const media = document.createElement('audio');
        media.muted = true;
        media.preload = 'none';
        // No source URL is attached: this is a drawing of compact cached
        // peaks. The shared Web Audio graph is the sole playback owner.
        wavesurferRef.current = WaveSurfer.create({
          container: waveformRef.current,
          media,
          peaks,
          duration: audioDuration,
          width: Math.max(1, widthRef.current),
          waveColor,
          progressColor: waveColor,
          cursorWidth: 0,
          barWidth: 1,
          barRadius: 1,
          barGap: 1,
          height: 28,
          normalize: true,
          interact: false,
        });
      })
      .catch((error) => {
        if (!disposed && !(error instanceof DOMException && error.name === 'AbortError')) {
          console.error('[StoryWaveform] Audio could not be prepared:', error);
        }
      });
    return () => {
      disposed = true;
      release();
      wavesurferRef.current?.destroy();
      wavesurferRef.current = null;
    };
  }, [sourceKey, sourceUrl]);

  useEffect(() => {
    wavesurferRef.current?.setOptions({ width: Math.max(1, fullWaveformWidth) });
  }, [fullWaveformWidth]);

  return (
    <div className="w-full h-full opacity-60 overflow-hidden">
      <div
        ref={waveformRef}
        style={{ width: `${fullWaveformWidth}px`, transform: `translateX(-${offsetX}px)` }}
        className="h-full"
      />
    </div>
  );
}
