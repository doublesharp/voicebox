import { useCallback, useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { useServerStore } from '@/stores/serverStore';

interface QualityReport {
  decoded_audio_sha256?: string;
  suggested_segments?: Array<{
    rank: number;
    start_seconds: number;
    end_seconds: number;
    duration_seconds: number;
    clipped_fraction: number;
    silence_fraction: number;
    score: number;
  }>;
  duration_seconds: number;
  clipped_fraction: number;
  silence_fraction: number;
  peak_dbfs: number;
  rms_dbfs: number;
  issues: Array<{ code: string; message: string; suggestion: string }>;
}
interface Props {
  file?: File | null;
  audioUrl?: string;
  referenceText: string;
  language?: string;
}
const MAX_BYTES = 25 * 1024 * 1024;
function normalized(text: string) {
  return text
    .normalize('NFKC')
    .toLocaleLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}
async function jsonResponse(response: Response) {
  const body = await response.json().catch(() => null);
  if (!response.ok)
    throw new Error(
      typeof body?.detail === 'string' ? body.detail : `Sample check failed (${response.status}).`,
    );
  return body;
}
function validReport(value: QualityReport) {
  return (
    value &&
    ['duration_seconds', 'clipped_fraction', 'silence_fraction', 'peak_dbfs', 'rms_dbfs'].every(
      (key) =>
        typeof value[key as keyof QualityReport] === 'number' &&
        Number.isFinite(value[key as keyof QualityReport]),
    ) &&
    (value.suggested_segments === undefined ||
      (Array.isArray(value.suggested_segments) &&
        value.suggested_segments.length <= 3 &&
        value.suggested_segments.every(
          (item) =>
            item &&
            Number.isFinite(item.start_seconds) &&
            Number.isFinite(item.end_seconds) &&
            item.start_seconds >= 0 &&
            item.end_seconds > item.start_seconds &&
            item.end_seconds <= value.duration_seconds,
        ))) &&
    Array.isArray(value.issues) &&
    value.issues.every(
      (item) =>
        item &&
        typeof item.code === 'string' &&
        typeof item.message === 'string' &&
        typeof item.suggestion === 'string',
    )
  );
}

export function SampleQuality({ file, audioUrl, referenceText, language }: Props) {
  const server = useServerStore((state) => state.serverUrl).replace(/\/$/, '');
  const identity = useRef({ file, audioUrl, language, server, epoch: 0 });
  if (
    identity.current.file !== file ||
    identity.current.audioUrl !== audioUrl ||
    identity.current.language !== language ||
    identity.current.server !== server
  ) {
    identity.current = { file, audioUrl, language, server, epoch: identity.current.epoch + 1 };
  }
  const epoch = identity.current.epoch;
  const mounted = useRef(true);
  const requests = useRef(new Set<AbortController>());
  const pending = useRef<{ epoch: number; request: AbortController } | null>(null);
  const cache = useRef<{ epoch: number; file: File } | null>(null);
  const [report, setReport] = useState<{ epoch: number; value: QualityReport } | null>(null);
  const [recognized, setRecognized] = useState<{ epoch: number; text: string } | null>(null);
  const [busy, setBusy] = useState<{ epoch: number; type: 'levels' | 'transcript' } | null>(null);
  const [error, setError] = useState<{ epoch: number; message: string } | null>(null);
  const audio = useRef<HTMLAudioElement | null>(null);
  const auditionEnd = useRef<number | null>(null);
  const [auditionUrl, setAuditionUrl] = useState('');
  useEffect(() => {
    const local = file ? URL.createObjectURL(file) : (audioUrl ?? '');
    setAuditionUrl(local);
    return () => {
      audio.current?.pause();
      auditionEnd.current = null;
      if (file && local) URL.revokeObjectURL(local);
    };
  }, [file, audioUrl]);
  const isCurrent = useCallback(
    (requestEpoch: number) => mounted.current && identity.current.epoch === requestEpoch,
    [],
  );
  const getFile = useCallback(
    async (signal: AbortSignal, requestEpoch: number) => {
      if (file) {
        if (file.size > MAX_BYTES) throw new Error('Reference audio must be no larger than 25 MB.');
        return file;
      }
      if (cache.current?.epoch === requestEpoch) return cache.current.file;
      if (!audioUrl) throw new Error('Choose a reference recording first.');
      const response = await fetch(audioUrl, { signal });
      if (!response.ok) throw new Error(`Could not read the sample (${response.status}).`);
      if (Number(response.headers.get('Content-Length')) > MAX_BYTES)
        throw new Error('Reference audio must be no larger than 25 MB.');
      const blob = await response.blob();
      if (blob.size > MAX_BYTES) throw new Error('Reference audio must be no larger than 25 MB.');
      const result = new File([blob], 'reference.wav', { type: blob.type || 'audio/wav' });
      if (isCurrent(requestEpoch)) cache.current = { epoch: requestEpoch, file: result };
      return result;
    },
    [file, audioUrl, isCurrent],
  );

  const check = useCallback(
    async (type: 'levels' | 'transcript') => {
      const requestEpoch = identity.current.epoch;
      if (pending.current?.epoch === requestEpoch && !pending.current.request.signal.aborted)
        return;
      const request = new AbortController();
      pending.current = { epoch: requestEpoch, request };
      requests.current.add(request);
      setBusy({ epoch: requestEpoch, type });
      setError(null);
      try {
        const recording = await getFile(request.signal, requestEpoch);
        if (!isCurrent(requestEpoch)) return;
        const body = new FormData();
        body.append('file', recording);
        if (type === 'transcript' && language) body.append('language', language);
        const result = await jsonResponse(
          await fetch(`${server}/sample-quality/${type === 'levels' ? 'analyze' : 'transcribe'}`, {
            method: 'POST',
            body,
            signal: request.signal,
          }),
        );
        if (!isCurrent(requestEpoch) || request.signal.aborted) return;
        if (type === 'levels') {
          if (!validReport(result))
            throw new Error('The sample check returned an invalid report. Try checking again.');
          setReport({ epoch: requestEpoch, value: result });
        } else {
          if (typeof result?.text !== 'string')
            throw new Error('The transcription model returned invalid text. Compare by listening.');
          setRecognized({ epoch: requestEpoch, text: result.text });
        }
      } catch (reason) {
        if (isCurrent(requestEpoch) && !request.signal.aborted)
          setError({
            epoch: requestEpoch,
            message: reason instanceof Error ? reason.message : 'Sample check failed. Try again.',
          });
      } finally {
        requests.current.delete(request);
        if (pending.current?.request === request) {
          pending.current = null;
          if (isCurrent(requestEpoch)) setBusy(null);
        }
      }
    },
    [getFile, language, server, isCurrent],
  );

  useEffect(() => {
    mounted.current = true;
    if (file) void check('levels');
    return () => {
      mounted.current = false;
      for (const request of requests.current) request.abort();
      requests.current.clear();
    };
  }, [file, check]);

  if (!file && !audioUrl) return null;
  const currentReport = report?.epoch === epoch ? report.value : null;
  const transcript = recognized?.epoch === epoch ? recognized.text : null;
  const currentBusy = busy?.epoch === epoch ? busy.type : null;
  const matches =
    transcript !== null &&
    !!normalized(referenceText) &&
    normalized(referenceText) === normalized(transcript);
  return (
    <section
      aria-label="Reference sample quality"
      className="rounded-lg border border-border/70 bg-muted/20 p-3 text-xs space-y-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-medium text-sm">Reference quality</p>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={!!currentBusy}
          onClick={() => check('levels')}
        >
          {currentBusy === 'levels'
            ? 'Checking sample…'
            : currentReport
              ? 'Check again'
              : 'Check sample'}
        </Button>
      </div>
      {currentBusy === 'levels' && <output>Measuring recording levels…</output>}
      {error?.epoch === epoch && (
        <p role="alert" className="text-destructive">
          {error.message}
        </p>
      )}
      {currentReport && (
        <>
          <p className="text-muted-foreground">
            {currentReport.duration_seconds.toFixed(1)} s · Peak{' '}
            {currentReport.peak_dbfs.toFixed(1)} dBFS · Quiet time{' '}
            {Math.round(currentReport.silence_fraction * 100)}%
          </p>
          {currentReport.issues.length ? (
            <ul className="space-y-2 list-disc pl-4">
              {currentReport.issues.map((item) => (
                <li key={item.code}>
                  <strong>{item.message}</strong> {item.suggestion}
                </li>
              ))}
            </ul>
          ) : (
            <p>No level warnings found. Listen for background noise, music, or other speakers.</p>
          )}
        </>
      )}
      {!!currentReport?.suggested_segments?.length && (
        <div className="space-y-2">
          <p className="font-medium">Suggested reference intervals</p>
          <p>
            Ranked by audible level, quiet time and clipping. These do not detect speaker identity
            or sentence boundaries. Listen, then trim a copy and match its transcript; the original
            is kept.
          </p>
          {/* biome-ignore lint/a11y/useMediaCaption: The supplied reference transcript remains visible; automatic captions would imply verified wording. */}
          <audio
            ref={(element) => {
              if (audio.current && audio.current !== element) audio.current.pause();
              audio.current = element;
            }}
            src={auditionUrl}
            controls
            preload="metadata"
            aria-label="Original reference audition"
            onTimeUpdate={() => {
              if (
                audio.current &&
                auditionEnd.current !== null &&
                audio.current.currentTime >= auditionEnd.current
              ) {
                audio.current.pause();
                auditionEnd.current = null;
              }
            }}
          />
          {currentReport.suggested_segments.map((segment) => (
            <Button
              key={segment.rank}
              type="button"
              size="sm"
              variant="outline"
              onClick={() => {
                if (!audio.current) return;
                auditionEnd.current = segment.end_seconds;
                audio.current.currentTime = segment.start_seconds;
                void audio.current.play().catch(() => {
                  if (isCurrent(epoch))
                    setError({
                      epoch,
                      message:
                        'Playback was blocked. Use the original reference controls to listen.',
                    });
                });
              }}
            >
              Audition suggestion {segment.rank}: {segment.start_seconds.toFixed(1)}–
              {segment.end_seconds.toFixed(1)} s
            </Button>
          ))}
        </div>
      )}
      <div className="border-t border-border/60 pt-3 space-y-2">
        {transcript === null ? (
          <p className="text-muted-foreground">
            Transcript has not been checked against this recording.
          </p>
        ) : (
          <>
            <p className={matches ? '' : 'font-medium'}>
              {matches
                ? 'Normalized text matches the automatic transcript.'
                : 'Reference text differs from the automatic transcript. Listen and correct any missing or extra words.'}
            </p>
            <p>
              <span className="font-medium">Reference: </span>
              {referenceText || '(empty)'}
            </p>
            <p>
              <span className="font-medium">Recognized: </span>
              {transcript || '(no speech recognized)'}
            </p>
          </>
        )}
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={!!currentBusy || !referenceText.trim()}
          onClick={() => check('transcript')}
        >
          {currentBusy === 'transcript' ? 'Checking transcript…' : 'Check transcript'}
        </Button>
        <p className="text-muted-foreground">
          Uses a downloaded Whisper model. Automatic transcription can be wrong; confirm by
          listening.
        </p>
      </div>
      <p className="text-muted-foreground">
        Quality checks are advisory. The noise check uses recording levels and may flag continuous
        speech.
      </p>
    </section>
  );
}
