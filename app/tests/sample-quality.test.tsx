import { afterAll, afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

mock.module('../src/stores/serverStore', () => ({
  useServerStore: (selector: (value: { serverUrl: string }) => unknown) =>
    selector({ serverUrl: 'http://local' }),
}));
const oldFetch = globalThis.fetch;
let renderer: ReactTestRenderer | null = null;
const requests: string[] = [];
const report = {
  duration_seconds: 5,
  channels: 1,
  peak_dbfs: 0,
  rms_dbfs: -20,
  clipped_fraction: 0.05,
  silence_fraction: 0.3,
  leading_silence_seconds: 1,
  trailing_silence_seconds: 0.5,
  issues: [
    {
      code: 'clipping',
      message: 'Peaks reach the digital ceiling.',
      suggestion: 'Lower the input gain and record again.',
    },
  ],
};
const { SampleQuality } = await import('../src/components/VoiceProfiles/SampleQuality');
const file = (name: string) => new File(['audio'], name, { type: 'audio/wav' });
const text = () => JSON.stringify(renderer?.toJSON());
const button = (label: string) =>
  renderer!.root.findAllByType('button').find((node) => node.children.join('').includes(label))!;

beforeEach(() => {
  requests.length = 0;
  globalThis.fetch = mock(async (url: string | URL | Request) => {
    requests.push(String(url));
    return Response.json(
      String(url).endsWith('/transcribe') ? { text: 'Different words' } : report,
    );
  }) as typeof fetch;
});
afterEach(() => {
  act(() => renderer?.unmount());
  renderer = null;
});
afterAll(() => {
  globalThis.fetch = oldFetch;
  mock.restore();
});

test('a selected recording gets actionable level guidance before save without running speech recognition', async () => {
  await act(async () => {
    renderer = create(<SampleQuality file={file('a.wav')} referenceText="Hello world" />);
  });
  expect(text()).toContain('Lower the input gain');
  expect(text()).toContain('Transcript has not been checked');
  expect(requests).toEqual(['http://local/sample-quality/analyze']);
});

test('transcript comparison is explicit and leaves the supplied reference visible', async () => {
  await act(async () => {
    renderer = create(
      <SampleQuality file={file('a.wav')} referenceText="Hello world" language="en" />,
    );
  });
  await act(async () => {
    await button('Check transcript').props.onClick();
  });
  expect(requests.at(-1)).toBe('http://local/sample-quality/transcribe');
  expect(text()).toContain('Different words');
  expect(text()).toContain('Hello world');
  expect(text()).toContain('differs');
});

test('changing the reference text recomputes mismatch against the same recognized recording', async () => {
  const sample = file('a.wav');
  await act(async () => {
    renderer = create(<SampleQuality file={sample} referenceText="Hello world" />);
  });
  await act(async () => {
    await button('Check transcript').props.onClick();
  });
  await act(async () => {
    renderer!.update(<SampleQuality file={sample} referenceText="Different words!" />);
  });
  expect(text()).toContain('matches');
  expect(requests).toHaveLength(2);
});

test('late recognition cannot label a different recording', async () => {
  let resolve!: (result: Response) => void;
  const pending = new Promise<Response>((r) => {
    resolve = r;
  });
  globalThis.fetch = mock(async (url: string | URL | Request) =>
    String(url).endsWith('/transcribe') ? pending : Response.json(report),
  ) as typeof fetch;
  await act(async () => {
    renderer = create(<SampleQuality file={file('a.wav')} referenceText="Hello world" />);
  });
  let checking!: Promise<void>;
  act(() => {
    checking = button('Check transcript').props.onClick();
  });
  await act(async () => {
    renderer!.update(<SampleQuality file={file('b.wav')} referenceText="New reference" />);
  });
  await act(async () => {
    resolve(Response.json({ text: 'Obsolete transcript' }));
    await checking;
  });
  expect(text()).not.toContain('Obsolete transcript');
  expect(text()).toContain('Transcript has not been checked');
});

test('saved samples wait for an explicit check and surface actionable missing-model errors', async () => {
  globalThis.fetch = mock(async (url: string | URL | Request) => {
    requests.push(String(url));
    if (String(url).endsWith('/transcribe'))
      return Response.json({ detail: 'Download Whisper in Server → Models.' }, { status: 409 });
    if (String(url).endsWith('/sample.wav'))
      return new Response('audio', { headers: { 'Content-Type': 'audio/wav' } });
    return Response.json(report);
  }) as typeof fetch;
  await act(async () => {
    renderer = create(
      <SampleQuality audioUrl="http://local/sample.wav" referenceText="Hello world" />,
    );
  });
  expect(requests).toHaveLength(0);
  await act(async () => {
    await button('Check sample').props.onClick();
  });
  expect(text()).toContain('Lower the input gain');
  await act(async () => {
    await button('Check transcript').props.onClick();
  });
  expect(text()).toContain('Download Whisper');
});
