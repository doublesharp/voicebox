import { useEffect } from 'react';
import { createAudioKeepAlive } from '@/lib/audio/audioKeepAlive';
import { debug } from '@/lib/utils/debug';

// WKWebView can lose its CoreAudio output after prolonged backgrounding (#41).
// Retain an unmuted <audio> session, but feed it continuous zero-valued PCM.
// The short WAV loop requires AVFoundation to seek every second; a live
// stream has no clip boundary to restart.
export function AudioKeepAlive() {
  useEffect(() => {
    const output = createAudioKeepAlive();
    const tryPlay = () => {
      output.start().catch((err) => {
        debug.log('[AudioKeepAlive] start blocked (will retry on next gesture):', err);
      });
    };

    tryPlay();

    // Autoplay may be blocked until first user interaction — re-attempt then.
    window.addEventListener('pointerdown', tryPlay);
    window.addEventListener('keydown', tryPlay);

    // Recover either a paused element or an interrupted/suspended context.
    const onWake = () => {
      if (!document.hidden) tryPlay();
    };
    document.addEventListener('visibilitychange', onWake);
    window.addEventListener('focus', onWake);
    window.addEventListener('pageshow', onWake);

    return () => {
      window.removeEventListener('pointerdown', tryPlay);
      window.removeEventListener('keydown', tryPlay);
      document.removeEventListener('visibilitychange', onWake);
      window.removeEventListener('focus', onWake);
      window.removeEventListener('pageshow', onWake);
      output.dispose().catch((err) => {
        debug.log('[AudioKeepAlive] cleanup failed:', err);
      });
    };
  }, []);

  return null;
}
