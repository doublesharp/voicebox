export interface WaveformData {
  sample_rate: number;
  frames: number;
  channels: number;
  peaks: [number, number][];
}

const requests = new Map<string, Promise<WaveformData>>();
export function waveformRequest(url: string, fetcher: typeof fetch = fetch): Promise<WaveformData> {
  const existing = requests.get(url);
  if (existing) return existing;
  const request = fetcher(url)
    .then(async (response) => {
      if (!response.ok) throw new Error('Source unavailable');
      const data = (await response.json()) as WaveformData;
      if (
        !Number.isFinite(data.frames) ||
        data.frames <= 0 ||
        !Number.isFinite(data.sample_rate) ||
        data.sample_rate <= 0 ||
        !Array.isArray(data.peaks) ||
        !data.peaks.length ||
        data.peaks.some(
          (pair) => !Array.isArray(pair) || pair.length !== 2 || !pair.every(Number.isFinite),
        )
      )
        throw new Error('Invalid waveform');
      return data;
    })
    .catch((error: unknown) => {
      if (requests.get(url) === request) requests.delete(url);
      throw error;
    });
  requests.set(url, request);
  if (requests.size > 128) requests.delete(requests.keys().next().value as string);
  return request;
}

export function waveformView(
  frames: number,
  nativeRate: number,
  projectRate: number,
  trimStart: number,
  trimEnd: number,
  bins: number,
) {
  const total = Math.round((frames * projectRate) / nativeRate);
  const start = Math.min(bins, Math.max(0, (trimStart / total) * bins));
  const end = Math.min(bins, Math.max(start, ((total - trimEnd) / total) * bins));
  return { start, width: end - start };
}
