import { describe, expect, test } from 'bun:test';
import { waveformRequest, waveformView } from '../src/features/studio/waveform-data';

describe('waveform data', () => {
  test('maps project-rate trims to the native source envelope', () => {
    expect(waveformView(48000, 24000, 48000, 4800, 9600, 2048)).toEqual({
      start: 102.4,
      width: 1740.8,
    });
  });
  test('shares in-flight and completed requests and retries failures', async () => {
    let calls = 0;
    const fetcher = (async () => {
      calls++;
      return calls === 1
        ? new Response('', { status: 503 })
        : Response.json({ sample_rate: 1, frames: 1, channels: 1, peaks: [[-1, 1]] });
    }) as typeof fetch;
    await expect(waveformRequest('http://wave-test', fetcher)).rejects.toThrow();
    const first = waveformRequest('http://wave-test', fetcher);
    expect(waveformRequest('http://wave-test', fetcher)).toBe(first);
    await first;
    await waveformRequest('http://wave-test', fetcher);
    expect(calls).toBe(2);
  });
});
