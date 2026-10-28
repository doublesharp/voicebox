import { afterEach, beforeEach, describe, expect, mock, test } from 'bun:test';
import { createElement } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { PlatformProvider } from '../src/platform/PlatformContext';
import type { Platform } from '../src/platform/types';

const values = new Map<string, string>();
const storage = {
  getItem: (key: string) => values.get(key) ?? null,
  setItem: (key: string, value: string) => {
    values.set(key, value);
  },
  removeItem: (key: string) => {
    values.delete(key);
  },
};
const testWindow = Object.assign(new EventTarget(), {
  setInterval,
  clearInterval,
  localStorage: storage,
});
Object.defineProperty(globalThis, 'window', { configurable: true, value: testWindow });
Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage });

mock.module('../src/lib/utils/audio', () => ({ convertToWav: async (blob: Blob) => blob }));

const { useMicrophoneStore } = await import('../src/stores/microphoneStore');
const { useAudioRecording } = await import('../src/lib/hooks/useAudioRecording');
const { useMicrophonePreview } = await import('../src/lib/hooks/useMicrophonePreview');

const recorders: Recorder[] = [];

class Recorder {
  constructor() {
    recorders.push(this);
  }
  static isTypeSupported() {
    return true;
  }
  state = 'inactive';
  onstop: (() => void) | null = null;
  start() {
    this.state = 'recording';
  }
  stop() {
    this.state = 'inactive';
    queueMicrotask(() => this.onstop?.());
  }
}
Object.defineProperty(globalThis, 'MediaRecorder', { configurable: true, value: Recorder });

function microphone(deviceId: string) {
  const stop = mock(() => {});
  const track = { stop, getSettings: () => ({ deviceId }) };
  return {
    stream: { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream,
    stop,
  };
}

let renderer: ReactTestRenderer | null = null;
let recording!: ReturnType<typeof useAudioRecording>;
let preview!: ReturnType<typeof useMicrophonePreview>;

function RecordingProbe() {
  recording = useAudioRecording();
  return null;
}
function PreviewProbe() {
  preview = useMicrophonePreview(true);
  return null;
}

function mountRecording() {
  act(() => {
    renderer = create(
      <PlatformProvider platform={{ metadata: { isTauri: true } } as Platform}>
        <RecordingProbe />
      </PlatformProvider>,
    );
  });
}

function useDevices(getUserMedia: (constraints: MediaStreamConstraints) => Promise<MediaStream>) {
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } });
}

beforeEach(() => {
  useMicrophoneStore.getState().setMicrophone('shure-usb', 'Shure USB');
});
afterEach(async () => {
  await act(async () => {
    renderer?.unmount();
  });
  renderer = null;
});

describe('recording input lifecycle', () => {
  test('automatic input stop returns the recorder to idle and allows Stop safely', async () => {
    const { stream } = microphone('shure-usb');
    useDevices(async () => stream);
    mountRecording();
    await act(async () => {
      await recording.startRecording();
    });
    await act(async () => {
      recorders[recorders.length - 1].stop();
    });
    expect(recording.isRecording).toBe(false);
    expect(() => recording.stopRecording()).not.toThrow();
  });

  test('records from the saved Shure input and exposes that same stream to the waveform', async () => {
    const { stream } = microphone('shure-usb');
    const acquire = mock(async (_constraints: MediaStreamConstraints) => stream);
    useDevices(acquire);
    mountRecording();
    await act(async () => {
      await recording.startRecording();
    });
    expect(acquire.mock.calls[0][0].audio).toMatchObject({ deviceId: { exact: 'shure-usb' } });
    expect(recording.isRecording).toBe(true);
    expect(recording.audioStream).toBe(stream);
    await act(async () => {
      recording.cancelRecording();
    });
  });

  test('canceling a pending request stops a late-granted stream without starting a recording', async () => {
    const { stream, stop } = microphone('shure-usb');
    let grant!: (stream: MediaStream) => void;
    useDevices(
      () =>
        new Promise((resolve) => {
          grant = resolve;
        }),
    );
    mountRecording();
    let pending!: Promise<void>;
    act(() => {
      pending = recording.startRecording();
    });
    expect(recording.isStarting).toBe(true);
    act(() => {
      recording.cancelRecording();
    });
    await act(async () => {
      grant(stream);
      await pending;
    });
    expect(recording.isRecording).toBe(false);
    expect(recording.isStarting).toBe(false);
    expect(recording.audioStream).toBeNull();
    expect(stop).toHaveBeenCalledTimes(1);
  });

  test('a canceled old request cannot stop a newer recording', async () => {
    const old = microphone('shure-usb');
    const current = microphone('shure-usb');
    let grantOld!: (stream: MediaStream) => void;
    let calls = 0;
    useDevices(() =>
      ++calls === 1
        ? new Promise((resolve) => {
            grantOld = resolve;
          })
        : Promise.resolve(current.stream),
    );
    mountRecording();
    let pending!: Promise<void>;
    act(() => {
      pending = recording.startRecording();
    });
    act(() => {
      recording.cancelRecording();
    });
    await act(async () => {
      await recording.startRecording();
    });
    await act(async () => {
      grantOld(old.stream);
      await pending;
    });
    expect(recording.isRecording).toBe(true);
    expect(recording.audioStream).toBe(current.stream);
    expect(current.stop).not.toHaveBeenCalled();
    expect(old.stop).toHaveBeenCalledTimes(1);
    await act(async () => {
      recording.cancelRecording();
    });
  });

  test('the dictation webview picks up a microphone change saved by the main window', async () => {
    storage.setItem(
      'voicebox-microphone',
      JSON.stringify({
        state: { deviceId: 'another-shure', deviceLabel: 'Shure MV7' },
        version: 0,
      }),
    );
    const event = Object.assign(new Event('storage'), { key: 'voicebox-microphone' });
    await act(async () => {
      testWindow.dispatchEvent(event);
    });
    expect(useMicrophoneStore.getState().deviceId).toBe('another-shure');
    const { stream } = microphone('another-shure');
    const acquire = mock(async (_constraints: MediaStreamConstraints) => stream);
    useDevices(acquire);
    mountRecording();
    await act(async () => {
      await recording.startRecording();
    });
    expect(acquire.mock.calls[0][0].audio).toMatchObject({ deviceId: { exact: 'another-shure' } });
    await act(async () => {
      recording.cancelRecording();
    });
  });

  test('changing preview input releases the old stream and closing releases the new stream', async () => {
    const old = microphone('shure-usb');
    const current = microphone('second-usb');
    useDevices(async (constraints) =>
      (constraints.audio as MediaTrackConstraints).deviceId &&
      ((constraints.audio as MediaTrackConstraints).deviceId as ConstrainDOMStringParameters)
        .exact === 'shure-usb'
        ? old.stream
        : current.stream,
    );
    await act(async () => {
      renderer = create(createElement(PreviewProbe));
    });
    expect(preview.stream).toBe(old.stream);
    await act(async () => {
      useMicrophoneStore.getState().setMicrophone('second-usb', 'Second USB');
    });
    expect(old.stop).toHaveBeenCalled();
    expect(preview.stream).toBe(current.stream);
    await act(async () => {
      renderer?.unmount();
    });
    renderer = null;
    expect(current.stop).toHaveBeenCalled();
  });
});
