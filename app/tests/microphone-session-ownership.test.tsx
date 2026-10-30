import { afterAll, afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

let convert: (blob: Blob) => Promise<Blob> = async (blob) => blob;
mock.module('../src/lib/utils/audio', () => ({ convertToWav: (blob: Blob) => convert(blob) }));
mock.module('../src/platform/PlatformContext', () => ({
  usePlatform: () => ({ metadata: { isTauri: false } }),
}));
const useMicrophoneStore = Object.assign((selector: any) => selector({ deviceId: null }), {
  getState: () => ({ deviceId: null }),
});
mock.module('../src/stores/microphoneStore', () => ({ useMicrophoneStore }));
const { useAudioRecording } = await import('../src/lib/hooks/useAudioRecording');
const oldNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
const oldWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
const oldRecorder = Object.getOwnPropertyDescriptor(globalThis, 'MediaRecorder');
Object.defineProperty(globalThis, 'window', {
  configurable: true,
  value: { setInterval: () => 1 },
});
const tracks: Array<{ stop: ReturnType<typeof mock> }> = [];
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  value: {
    mediaDevices: {
      getUserMedia: async () => {
        const track = { stop: mock(() => {}), getSettings: () => ({ deviceId: 'mic' }) };
        tracks.push(track);
        return { getTracks: () => [track], getAudioTracks: () => [track] };
      },
    },
  },
});
const recorders: Recorder[] = [];
let failOnStart = false;
let throwOnStart = false;
class Recorder {
  static isTypeSupported() {
    return true;
  }
  state = 'inactive';
  mimeType = 'audio/webm';
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => Promise<void> | void) | null = null;
  onerror: (() => void) | null = null;
  constructor() {
    recorders.push(this);
  }
  start() {
    if (throwOnStart) throw new Error('recorder refused to start');
    this.state = 'recording';
    if (failOnStart) this.onerror?.();
  }
  stop() {
    this.state = 'inactive';
  }
  emit(text: string) {
    this.ondataavailable?.({ data: new Blob([text]) });
  }
  async finish() {
    await this.onstop?.();
  }
}
Object.defineProperty(globalThis, 'MediaRecorder', { configurable: true, value: Recorder });
let view: ReactTestRenderer | null = null;
let recording: ReturnType<typeof useAudioRecording>;
const completed = mock((_blob: Blob, _duration?: number) => {});
function Probe() {
  recording = useAudioRecording({ onRecordingComplete: completed });
  return null;
}
function mount() {
  act(() => {
    view = create(<Probe />);
  });
}
async function start() {
  await act(async () => {
    await recording.startRecording();
  });
}
beforeEach(() => {
  failOnStart = false;
  throwOnStart = false;
  recorders.length = 0;
  tracks.length = 0;
  completed.mockClear();
  convert = async (blob) => blob;
});
afterEach(() => {
  if (view) act(() => view?.unmount());
  view = null;
});
afterAll(() => {
  mock.restore();
  for (const [key, descriptor] of [
    ['navigator', oldNavigator],
    ['window', oldWindow],
    ['MediaRecorder', oldRecorder],
  ] as const) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else Reflect.deleteProperty(globalThis, key);
  }
});

test('a cancelled recorder cannot publish after a new recording starts', async () => {
  mount();
  await start();
  const old = recorders[0];
  act(() => recording.cancelRecording());
  await start();
  await act(async () => {
    old.emit('old');
    await old.finish();
  });
  expect(completed).not.toHaveBeenCalled();
  expect(recording.isRecording).toBe(true);
  expect(tracks[1].stop).not.toHaveBeenCalled();
});

test('late chunks stay with their own recording when sessions overlap', async () => {
  mount();
  await start();
  const old = recorders[0];
  act(() => recording.stopRecording());
  await start();
  const current = recorders[1];
  act(() => current.emit('new'));
  await act(async () => {
    old.emit('old');
    await old.finish();
  });
  expect(completed).toHaveBeenCalledTimes(1);
  expect(await completed.mock.calls[0][0].text()).toBe('old');
  expect(recording.isRecording).toBe(true);
  expect(tracks[1].stop).not.toHaveBeenCalled();
  act(() => recording.stopRecording());
  await act(async () => {
    await current.finish();
  });
  expect(await completed.mock.calls[1][0].text()).toBe('new');
});

test('cancel while WAV conversion is pending suppresses the result', async () => {
  let resolveConversion!: (blob: Blob) => void;
  convert = () =>
    new Promise((resolve) => {
      resolveConversion = resolve;
    });
  mount();
  await start();
  const old = recorders[0];
  act(() => recording.stopRecording());
  let finishing!: Promise<void>;
  act(() => {
    old.emit('old');
    finishing = old.finish();
  });
  act(() => recording.cancelRecording());
  await act(async () => {
    resolveConversion(new Blob(['wav']));
    await finishing;
  });
  expect(completed).not.toHaveBeenCalled();
});

test('unmount while WAV conversion is pending suppresses the result', async () => {
  let resolveConversion!: (blob: Blob) => void;
  convert = () =>
    new Promise((resolve) => {
      resolveConversion = resolve;
    });
  mount();
  await start();
  const old = recorders[0];
  act(() => recording.stopRecording());
  let finishing!: Promise<void>;
  act(() => {
    old.emit('old');
    finishing = old.finish();
  });
  act(() => view?.unmount());
  view = null;
  await act(async () => {
    resolveConversion(new Blob(['wav']));
    await finishing;
  });
  expect(completed).not.toHaveBeenCalled();
});

test('a failed microphone request returns the recorder to idle', async () => {
  const media = navigator.mediaDevices;
  const original = media.getUserMedia;
  media.getUserMedia = async () => {
    throw new DOMException('denied', 'NotAllowedError');
  };
  try {
    mount();
    await start();
    expect(recording.isStarting).toBe(false);
    expect(recording.isRecording).toBe(false);
    expect(recording.error).toContain('denied');
  } finally {
    media.getUserMedia = original;
  }
});

test('duration stops advancing when the user stops recording', async () => {
  const original = Date.now;
  let now = 1000;
  Date.now = () => now;
  try {
    mount();
    await start();
    const recorder = recorders[0];
    now = 2000;
    act(() => recording.stopRecording());
    now = 9000;
    await act(async () => {
      recorder.emit('clip');
      await recorder.finish();
    });
    expect(completed.mock.calls[0][1]).toBe(1);
  } finally {
    Date.now = original;
  }
});

test('a recorder error cannot publish a partial recording and releases its microphone', async () => {
  mount();
  await start();
  const recorder = recorders[0];
  await act(async () => {
    recorder.onerror?.();
    recorder.emit('partial');
    await recorder.finish();
  });
  expect(completed).not.toHaveBeenCalled();
  expect(recording.error).toBe('Recording error occurred');
  expect(recording.isRecording).toBe(false);
  expect(tracks[0].stop).toHaveBeenCalledTimes(1);
});

test('a synchronous recorder error during startup does not enter recording state', async () => {
  failOnStart = true;
  mount();
  await start();
  expect(recording.error).toBe('Recording error occurred');
  expect(recording.isStarting).toBe(false);
  expect(recording.isRecording).toBe(false);
  expect(tracks[0].stop).toHaveBeenCalledTimes(1);
});

test('a recorder start exception releases the microphone and permits a retry', async () => {
  throwOnStart = true;
  mount();
  await start();
  expect(recording.isStarting).toBe(false);
  expect(recording.isRecording).toBe(false);
  expect(recording.error).toContain('recorder refused');
  expect(tracks[0].stop).toHaveBeenCalledTimes(1);
  throwOnStart = false;
  await start();
  expect(recording.isRecording).toBe(true);
});

test('conversion failure returns the original audio for an uncancelled recording', async () => {
  convert = async () => {
    throw new Error('conversion failed');
  };
  mount();
  await start();
  const recorder = recorders[0];
  act(() => recording.stopRecording());
  await act(async () => {
    recorder.emit('original');
    await recorder.finish();
  });
  expect(completed).toHaveBeenCalledTimes(1);
  expect(await completed.mock.calls[0][0].text()).toBe('original');
});


test('stop while microphone permission is pending cancels startup and releases its late stream', async () => {
  const media = navigator.mediaDevices;
  const original = media.getUserMedia;
  let resolve!: (stream: MediaStream) => void;
  const pending = new Promise<MediaStream>((r) => { resolve = r; });
  const track = { stop: mock(() => {}), getSettings: () => ({ deviceId: 'mic' }) };
  media.getUserMedia = () => pending;
  try {
    mount();
    let starting!: Promise<void>;
    act(() => { starting = recording.startRecording(); });
    expect(recording.isStarting).toBe(true);
    act(() => recording.stopRecording());
    expect(recording.isStarting).toBe(false);
    await act(async () => {
      resolve({ getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream);
      await starting;
    });
    expect(track.stop).toHaveBeenCalledTimes(1);
    expect(recorders).toHaveLength(0);
    expect(recording.isRecording).toBe(false);
    expect(completed).not.toHaveBeenCalled();
  } finally {
    media.getUserMedia = original;
  }
});


test('restarting while microphone acquisition is pending releases only the old stream', async () => {
  const media = navigator.mediaDevices;
  const original = media.getUserMedia;
  let resolveOld!: (stream: MediaStream) => void;
  const pending = new Promise<MediaStream>((r) => { resolveOld = r; });
  const oldTrack = { stop: mock(() => {}), getSettings: () => ({ deviceId: 'old' }) };
  const newTrack = { stop: mock(() => {}), getSettings: () => ({ deviceId: 'new' }) };
  let requests = 0;
  media.getUserMedia = async () => {
    requests++;
    return requests === 1 ? pending : { getTracks: () => [newTrack], getAudioTracks: () => [newTrack] } as unknown as MediaStream;
  };
  try {
    mount();
    let oldStart!: Promise<void>;
    act(() => { oldStart = recording.startRecording(); });
    act(() => recording.cancelRecording());
    await act(async () => { await recording.startRecording(); });
    expect(recording.isRecording).toBe(true);
    await act(async () => {
      resolveOld({ getTracks: () => [oldTrack], getAudioTracks: () => [oldTrack] } as unknown as MediaStream);
      await oldStart;
    });
    expect(oldTrack.stop).toHaveBeenCalledTimes(1);
    expect(newTrack.stop).not.toHaveBeenCalled();
    expect(recorders).toHaveLength(1);
    expect(recording.isRecording).toBe(true);
    expect(completed).not.toHaveBeenCalled();
  } finally {
    media.getUserMedia = original;
  }
});
