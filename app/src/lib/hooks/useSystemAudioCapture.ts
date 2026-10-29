import { useCallback, useEffect, useRef, useState } from 'react';
import { usePlatform } from '@/platform/PlatformContext';

interface UseSystemAudioCaptureOptions {
  maxDurationSeconds?: number;
  onRecordingComplete?: (blob: Blob, duration?: number) => void;
}

interface CaptureSession {
  start: Promise<void>;
  stop: Promise<void> | null;
  discard: boolean;
  startedAt: number | null;
}

/** Native system audio capture, with one owned start/stop lifecycle at a time. */
export function useSystemAudioCapture({
  maxDurationSeconds = 29,
  onRecordingComplete,
}: UseSystemAudioCaptureOptions = {}) {
  const platform = usePlatform();
  const [isRecording, setIsRecording] = useState(false);
  const [duration, setDuration] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [isSupported, setIsSupported] = useState(false);
  const sessionRef = useRef<CaptureSession | null>(null);
  const timerRef = useRef<number | null>(null);
  const mountedRef = useRef(true);
  const completeRef = useRef(onRecordingComplete);
  completeRef.current = onRecordingComplete;

  useEffect(() => {
    let active = true;
    void platform.audio.isSystemAudioSupported().then(
      (supported) => {
        if (active) setIsSupported(supported);
      },
      () => {
        if (active) setIsSupported(false);
      },
    );
    return () => {
      active = false;
    };
  }, [platform]);

  const stopSession = useCallback(
    (session: CaptureSession): Promise<void> => {
      if (session.stop) return session.stop;
      if (timerRef.current !== null) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
      if (mountedRef.current) setIsRecording(false);

      session.stop = (async () => {
        let started = false;
        try {
          await session.start;
          started = true;
          const stoppedAt = Date.now();
          const blob = await platform.audio.stopSystemAudioCapture();
          if (!session.discard && mountedRef.current) {
            const recordedDuration =
              session.startedAt === null ? undefined : (stoppedAt - session.startedAt) / 1000;
            completeRef.current?.(blob, recordedDuration);
          }
        } catch (err) {
          if (started && !session.discard && mountedRef.current) {
            setError(err instanceof Error ? err.message : 'Failed to stop system audio capture.');
          }
        } finally {
          if (sessionRef.current === session) sessionRef.current = null;
        }
      })();
      return session.stop;
    },
    [platform],
  );

  const startRecording = useCallback(async () => {
    if (sessionRef.current) return;
    if (!platform.metadata.isTauri) {
      setError('System audio capture is only available in the desktop app.');
      return;
    }
    if (!isSupported) {
      setError('System audio capture is not supported on this platform.');
      return;
    }
    const session: CaptureSession = {
      start: Promise.resolve(),
      stop: null,
      discard: false,
      startedAt: null,
    };
    sessionRef.current = session;
    setError(null);
    setDuration(0);
    try {
      session.start = platform.audio.startSystemAudioCapture(maxDurationSeconds);
      await session.start;
      if (session.stop) {
        await session.stop;
        return;
      }
      if (session.discard || !mountedRef.current) {
        await stopSession(session);
        return;
      }
      session.startedAt = Date.now();
      setIsRecording(true);
      timerRef.current = window.setInterval(() => {
        if (session.startedAt === null) return;
        const elapsed = (Date.now() - session.startedAt) / 1000;
        setDuration(elapsed);
        if (elapsed >= maxDurationSeconds) void stopSession(session);
      }, 100);
    } catch (err) {
      if (!session.discard && mountedRef.current) {
        setError(
          err instanceof Error
            ? err.message
            : 'Failed to start system audio capture. Please check permissions.',
        );
        setIsRecording(false);
      }
      if (sessionRef.current === session && !session.stop) sessionRef.current = null;
    }
  }, [maxDurationSeconds, isSupported, platform, stopSession]);

  const stopRecording = useCallback(async () => {
    const session = sessionRef.current;
    if (session) await stopSession(session);
  }, [stopSession]);

  const cancelRecording = useCallback(async () => {
    const session = sessionRef.current;
    if (session) {
      session.discard = true;
      await stopSession(session);
    }
    if (mountedRef.current) setDuration(0);
  }, [stopSession]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      const session = sessionRef.current;
      if (session) {
        session.discard = true;
        void stopSession(session);
      }
    };
  }, [stopSession]);

  return {
    isRecording,
    duration,
    error,
    isSupported,
    startRecording,
    stopRecording,
    cancelRecording,
  };
}
