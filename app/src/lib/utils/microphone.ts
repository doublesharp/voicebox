/** Open an explicit input, falling back to the system default if it is unavailable. */
export async function requestMicrophone(
  mediaDevices: Pick<MediaDevices, 'getUserMedia'>,
  deviceId: string | null,
  signal?: AbortSignal,
  onFallback?: () => void,
): Promise<MediaStream> {
  signal?.throwIfAborted();
  const audio: MediaTrackConstraints = {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  };
  let stream: MediaStream;
  let usedFallback = false;
  try {
    stream = await mediaDevices.getUserMedia({
      audio: { ...audio, ...(deviceId ? { deviceId: { exact: deviceId } } : {}) },
    });
  } catch (error) {
    signal?.throwIfAborted();
    // Permission/security failures must remain errors, rather than retrying another input.
    if (
      !deviceId ||
      !(error instanceof Error) ||
      !['NotFoundError', 'OverconstrainedError', 'NotReadableError'].includes(error.name)
    ) {
      throw error;
    }
    stream = await mediaDevices.getUserMedia({ audio });
    usedFallback = true;
  }
  try {
    signal?.throwIfAborted();
    const actualDeviceId = stream.getAudioTracks()[0]?.getSettings().deviceId;
    if (!usedFallback && deviceId && actualDeviceId && actualDeviceId !== deviceId) {
      throw new DOMException('The selected microphone could not be opened.', 'NotFoundError');
    }
    if (usedFallback) onFallback?.();
    return stream;
  } catch (error) {
    stream.getTracks().forEach((track) => {
      track.stop();
    });
    throw error;
  }
}

export function microphoneErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    if (error.name === 'NotFoundError' || error.name === 'OverconstrainedError') {
      return 'The selected microphone is unavailable. Reconnect it or choose another microphone.';
    }
    if (error.name === 'NotAllowedError') {
      return 'Microphone access was denied. Allow access in your system or browser settings.';
    }
    return error.message;
  }
  return 'Could not open the microphone. Check its connection and permissions.';
}
