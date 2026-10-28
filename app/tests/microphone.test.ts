import { describe, expect, mock, test } from 'bun:test';
import { requestMicrophone } from '../src/lib/utils/microphone';

function microphone(deviceId = 'shure-usb') {
  const stop = mock(() => {});
  const track = { stop, getSettings: () => ({ deviceId }) };
  const stream = {
    getTracks: () => [track],
    getAudioTracks: () => [track],
  } as unknown as MediaStream;
  return { stream, stop };
}

describe('microphone acquisition', () => {
  test('requests the selected USB microphone instead of the system default', async () => {
    const { stream } = microphone();
    const getUserMedia = mock(async (_constraints: MediaStreamConstraints) => stream);
    await requestMicrophone({ getUserMedia }, 'shure-usb');
    expect(getUserMedia).toHaveBeenCalledWith({
      audio: {
        deviceId: { exact: 'shure-usb' },
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
  });

  test('leaves input selection to the system when explicitly using the default', async () => {
    const { stream } = microphone('built-in');
    const getUserMedia = mock(async (_constraints: MediaStreamConstraints) => stream);
    expect(await requestMicrophone({ getUserMedia }, null)).toBe(stream);
    expect(getUserMedia.mock.calls[0][0].audio).not.toHaveProperty('deviceId');
  });

  test('reports an unavailable selected mic without retrying the default mic', async () => {
    const error = new DOMException('USB microphone disconnected', 'NotFoundError');
    const getUserMedia = mock(async () => {
      throw error;
    });
    await expect(requestMicrophone({ getUserMedia }, 'shure-usb')).rejects.toBe(error);
    expect(getUserMedia).toHaveBeenCalledTimes(1);
  });

  test('stops a wrong input returned by the webview and refuses to record it', async () => {
    const { stream, stop } = microphone('built-in');
    await expect(
      requestMicrophone({ getUserMedia: async () => stream }, 'shure-usb'),
    ).rejects.toThrow();
    expect(stop).toHaveBeenCalledTimes(1);
  });

  test('releases a stream granted after its dialog closes', async () => {
    const { stream, stop } = microphone();
    let grant!: (stream: MediaStream) => void;
    const controller = new AbortController();
    const pending = requestMicrophone(
      {
        getUserMedia: () =>
          new Promise((resolve) => {
            grant = resolve;
          }),
      },
      'shure-usb',
      controller.signal,
    );
    controller.abort();
    grant(stream);
    await expect(pending).rejects.toThrow();
    expect(stop).toHaveBeenCalledTimes(1);
  });
});
