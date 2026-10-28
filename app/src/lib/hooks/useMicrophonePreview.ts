import { useEffect, useState } from 'react';
import {
  type MicrophoneProcessing,
  microphoneErrorMessage,
  requestMicrophone,
} from '@/lib/utils/microphone';
import { useMicrophoneStore } from '@/stores/microphoneStore';

export function useMicrophonePreview(
  enabled: boolean,
  processing: MicrophoneProcessing = 'processed',
) {
  const deviceId = useMicrophoneStore((state) => state.deviceId);
  const [preview, setPreview] = useState<{
    stream: MediaStream;
    deviceId: string | null;
    processing: MicrophoneProcessing;
  } | null>(null);
  const [usingDefaultMicrophone, setUsingDefaultMicrophone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled || !navigator.mediaDevices?.getUserMedia) return;
    const controller = new AbortController();
    let stream: MediaStream | null = null;
    setError(null);
    setUsingDefaultMicrophone(false);
    setPreview(null);
    requestMicrophone(
      navigator.mediaDevices,
      deviceId,
      controller.signal,
      () => setUsingDefaultMicrophone(true),
      processing,
    )
      .then((result) => {
        if (controller.signal.aborted) {
          result.getTracks().forEach((track) => {
            track.stop();
          });
          return;
        }
        stream = result;
        setPreview({ stream: result, deviceId, processing });
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
  }, [deviceId, enabled, processing]);

  const currentPreview =
    enabled && preview?.deviceId === deviceId && preview.processing === processing;
  return {
    stream: currentPreview ? preview.stream : null,
    error: enabled ? error : null,
    usingDefaultMicrophone: currentPreview && usingDefaultMicrophone,
  };
}
