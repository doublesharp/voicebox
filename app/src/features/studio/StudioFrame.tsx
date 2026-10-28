import { type ReactNode, useEffect } from 'react';
import { AudioKeepAlive } from '@/components/AudioPlayer/AudioKeepAlive';
import { TitleBarDragRegion } from '@/components/TitleBarDragRegion';
import { stopNative } from '@/lib/audio/playbackOwnership';
import { TOP_SAFE_AREA_PADDING } from '@/lib/constants/ui';
import { usePlatform } from '@/platform/PlatformContext';
import { usePlayerStore } from '@/stores/playerStore';

/** Studio owns its transport. Returning to legacy routes remounts their unchanged player. */
export function StudioFrame({ children }: { children: ReactNode }) {
  const platform = usePlatform();
  useEffect(() => {
    usePlayerStore.getState().setIsPlaying(false);
    usePlayerStore.getState().clearAutoPlayFlag();
    void stopNative(() => platform.audio.stopPlayback()).catch(() => {});
  }, [platform.audio]);
  return (
    <div
      className={`h-screen bg-background flex flex-col overflow-hidden ${TOP_SAFE_AREA_PADDING}`}
    >
      <TitleBarDragRegion />
      <AudioKeepAlive />
      {children}
    </div>
  );
}
