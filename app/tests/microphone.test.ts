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

  for (const name of ['NotFoundError', 'OverconstrainedError', 'NotReadableError']) {
    test(`falls back to the default mic and reports it after ${name}`, async () => {
      const { stream } = microphone('built-in');
      const fallback = mock(() => {});
      const getUserMedia = mock(async (constraints: MediaStreamConstraints) => {
        if ((constraints.audio as MediaTrackConstraints).deviceId) {
          throw new DOMException('Selected mic unavailable', name);
        }
        return stream;
      });
      expect(await requestMicrophone({ getUserMedia }, 'shure-usb', undefined, fallback)).toBe(
        stream,
      );
      expect(getUserMedia).toHaveBeenCalledTimes(2);
      expect(getUserMedia.mock.calls[1][0].audio).not.toHaveProperty('deviceId');
      expect(fallback).toHaveBeenCalledTimes(1);
    });
  }

  test('does not retry denied permission on another microphone', async () => {
    const error = new DOMException('Permission denied', 'NotAllowedError');
    const getUserMedia = mock(async () => {
      throw error;
    });
    await expect(requestMicrophone({ getUserMedia }, 'shure-usb')).rejects.toBe(error);
    expect(getUserMedia).toHaveBeenCalledTimes(1);
  });

  test('reports fallback failure without announcing a successful switch', async () => {
    const error = new DOMException('Default mic unavailable', 'NotReadableError');
    const fallback = mock(() => {});
    let calls = 0;
    const getUserMedia = mock(async () => {
      if (++calls === 1) throw new DOMException('Selected mic missing', 'NotFoundError');
      throw error;
    });
    await expect(
      requestMicrophone({ getUserMedia }, 'shure-usb', undefined, fallback),
    ).rejects.toBe(error);
    expect(getUserMedia).toHaveBeenCalledTimes(2);
    expect(fallback).not.toHaveBeenCalled();
  });

  test('canceling a failed selected request prevents the fallback request', async () => {
    const controller = new AbortController();
    const getUserMedia = mock(async () => {
      controller.abort();
      throw new DOMException('Selected mic missing', 'NotFoundError');
    });
    await expect(
      requestMicrophone({ getUserMedia }, 'shure-usb', controller.signal),
    ).rejects.toThrow();
    expect(getUserMedia).toHaveBeenCalledTimes(1);
  });

  test('releases a fallback stream granted after cancellation without notifying', async () => {
    const { stream, stop } = microphone('built-in');
    const fallback = mock(() => {});
    const controller = new AbortController();
    let grant!: (stream: MediaStream) => void;
    const pending = requestMicrophone(
      {
        getUserMedia: async (constraints) => {
          if ((constraints.audio as MediaTrackConstraints).deviceId) {
            throw new DOMException('Selected mic missing', 'NotFoundError');
          }
          return new Promise((resolve) => {
            grant = resolve;
          });
        },
      },
      'shure-usb',
      controller.signal,
      fallback,
    );
    await Promise.resolve();
    controller.abort();
    grant(stream);
    await expect(pending).rejects.toThrow();
    expect(stop).toHaveBeenCalledTimes(1);
    expect(fallback).not.toHaveBeenCalled();
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
