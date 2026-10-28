/** Open the same explicit input for recordings and live previews. */
export async function requestMicrophone(
  mediaDevices: Pick<MediaDevices, 'getUserMedia'>,
  deviceId: string | null,
  signal?: AbortSignal,
): Promise<MediaStream> {
  signal?.throwIfAborted();
  const stream = await mediaDevices.getUserMedia({
    audio: {
      ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });
  try {
    signal?.throwIfAborted();
    const actualDeviceId = stream.getAudioTracks()[0]?.getSettings().deviceId;
    if (deviceId && actualDeviceId && actualDeviceId !== deviceId) {
      throw new DOMException('The selected microphone could not be opened.', 'NotFoundError');
    }
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
