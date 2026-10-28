import { useEffect, useState } from 'react';
import { microphoneErrorMessage, requestMicrophone } from '@/lib/utils/microphone';
import { useMicrophoneStore } from '@/stores/microphoneStore';

export function useMicrophonePreview(enabled: boolean) {
  const deviceId = useMicrophoneStore((state) => state.deviceId);
  const [preview, setPreview] = useState<{ stream: MediaStream; deviceId: string | null } | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled || !navigator.mediaDevices?.getUserMedia) return;
    const controller = new AbortController();
    let stream: MediaStream | null = null;
    setError(null);
    setPreview(null);
    requestMicrophone(navigator.mediaDevices, deviceId, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) {
          result.getTracks().forEach((track) => {
            track.stop();
          });
          return;
        }
        stream = result;
        setPreview({ stream: result, deviceId });
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setError(microphoneErrorMessage(cause));
      });
    return () => {
      controller.abort();
      stream?.getTracks().forEach((track) => {
        track.stop();
      });
    };
  }, [deviceId, enabled]);

  return {
    stream: enabled && preview?.deviceId === deviceId ? preview.stream : null,
    error: enabled ? error : null,
  };
}
