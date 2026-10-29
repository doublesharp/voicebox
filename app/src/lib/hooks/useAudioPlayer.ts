import { useCallback, useEffect, useRef, useState } from 'react';
import { useToast } from '@/components/ui/use-toast';
import { createPlaybackOutput, type PlaybackOutput } from '@/lib/audio/playbackOutput';

interface PreviewSession {
  file: File;
  url: string;
  output: PlaybackOutput;
  removeListeners: () => void;
  generation: number;
}

export function useAudioPlayer() {
  const [isPlaying, setIsPlaying] = useState(false);
  const sessionRef = useRef<PreviewSession | null>(null);
  const { toast } = useToast();

  const release = useCallback((session: PreviewSession) => {
    if (sessionRef.current !== session) return;
    sessionRef.current = null;
    session.removeListeners();
    session.output.dispose();
    URL.revokeObjectURL(session.url);
  }, []);

  const cleanup = useCallback(() => {
    if (sessionRef.current) release(sessionRef.current);
    setIsPlaying(false);
  }, [release]);

  useEffect(
    () => () => {
      if (sessionRef.current) release(sessionRef.current);
    },
    [release],
  );

  const playPause = (file: File | null | undefined) => {
    if (!file) return;
    let session = sessionRef.current;
    if (session && session.file !== file) {
      release(session);
      session = null;
    }
    if (!session) {
      const output = createPlaybackOutput();
      const url = URL.createObjectURL(file);
      const audio = output.audio;
      audio.src = url;
      const created: PreviewSession = {
        file,
        url,
        output,
        removeListeners: () => {},
        generation: 0,
      };
      const ended = () => {
        if (sessionRef.current !== created) return;
        release(created);
        setIsPlaying(false);
      };
      const failed = () => {
        if (sessionRef.current !== created) return;
        release(created);
        setIsPlaying(false);
        toast({
          title: 'Playback error',
          description: 'Failed to play audio file',
          variant: 'destructive',
        });
      };
      audio.addEventListener('ended', ended);
      audio.addEventListener('error', failed);
      created.removeListeners = () => {
        audio.removeEventListener('ended', ended);
        audio.removeEventListener('error', failed);
      };
      sessionRef.current = created;
      session = created;
    }

    const current = session;
    const audio = current.output.audio;
    const request = ++current.generation;
    if (!audio.paused || current.output.isStarting()) {
      audio.pause();
      setIsPlaying(false);
      return;
    }
    setIsPlaying(true);
    void audio.play().catch(() => {
      // A rejected start from an old preview must not stop its replacement.
      if (sessionRef.current !== current || current.generation !== request) return;
      release(current);
      setIsPlaying(false);
      toast({
        title: 'Playback error',
        description: 'Failed to play audio file',
        variant: 'destructive',
      });
    });
  };

  return {
    isPlaying,
    playPause,
    cleanup,
  };
}
