import { useEffect, useMemo, useRef, useState } from 'react';
import { useStudio } from './context';
import { errorMessage } from './session';
import { type SpeechDifference, SpeechReviewClient } from './speech-review-client';
import type { StudioPassage } from './types';
import { Field, StudioButton } from './ui';

export function SpeechReviewPanel({
  passage,
  onRepair,
}: {
  passage: StudioPassage;
  onRepair: (takeId: string, suggestion: SpeechDifference) => void;
}) {
  const studio = useStudio();
  const { project, client, session } = studio;
  const [chosen, setChosen] = useState('');
  const takes = project.takes.filter((take) => take.passage_id === passage.id);
  const take = takes.find((item) => item.id === (chosen || passage.selected_take_id)) ?? takes[0];
  const asset = project.assets.find((item) => item.id === take?.asset_id);
  const transport = useMemo(() => new SpeechReviewClient(client.baseUrl), [client.baseUrl]);
  const identity = `${client.baseUrl}:${project.id}:${project.revision}:${take?.id ?? ''}`;
  const latest = useRef(identity);
  latest.current = identity;
  const request = useRef<AbortController | null>(null);
  const [error, setError] = useState('');
  const report = [...(project.document.speech_reviews ?? [])]
    .reverse()
    .find(
      (item) =>
        item.source.asset_hash === asset?.hash &&
        item.source.take_text === take?.text &&
        item.source.take_engine === take?.engine &&
        item.source.take_model === take?.model_size &&
        item.source.script_text === passage.text &&
        item.source.language === passage.language,
    );
  // biome-ignore lint/correctness/useExhaustiveDependencies: Every source revision or selected take revokes pending recognition.
  useEffect(() => {
    setError('');
    return () => {
      request.current?.abort();
    };
  }, [identity]);
  const disabled = !studio.canMutate || studio.actionBusy || !take;
  const check = () =>
    studio.runAction(async () => {
      setError('');
      if (!take || request.current) return;
      await session.flush();
      if (latest.current !== identity || session.getSnapshot().status !== 'saved') {
        setError('Save and reconcile the current project before speech review.');
        return;
      }
      const controller = new AbortController();
      request.current = controller;
      try {
        const result = await transport.recognize(
          project.id,
          take.id,
          project.revision,
          controller.signal,
        );
        if (controller.signal.aborted || latest.current !== identity) return;
        const saved = session.getSnapshot();
        if (
          saved.status !== 'saved' ||
          (saved.project && saved.project.revision !== project.revision)
        )
          throw new Error('The project changed during recognition. Check the current take again.');
        const acknowledged = await session.command('set_speech_review', {
          take_id: take.id,
          report: result,
        });
        if (!acknowledged)
          throw new Error(
            'Review storage was not acknowledged. Resolve the pending save before retrying.',
          );
      } catch (reason) {
        if (!controller.signal.aborted && latest.current === identity)
          setError(errorMessage(reason));
      } finally {
        if (request.current === controller) request.current = null;
      }
    });
  if (!takes.length) return null;
  return (
    <section aria-label="Local speech review" className="studio-advanced">
      <h4>Check generated speech</h4>
      <p className="studio-note">
        Compare a retained take with the current script using downloaded local Whisper. Differences
        are advisory: recognition can miss names, numbers or words. This keeps the script, selected
        take and original audio.
      </p>
      <Field label="Speech review take">
        <select
          aria-label="Speech review take"
          value={take?.id ?? ''}
          disabled={studio.actionBusy}
          onChange={(event) => setChosen(event.target.value)}
        >
          {takes.map((item, index) => (
            <option key={item.id} value={item.id}>
              Take {index + 1} · {item.text.slice(0, 70)}
            </option>
          ))}
        </select>
      </Field>
      <StudioButton disabled={disabled} onClick={check}>
        Check and save speech review
      </StudioButton>
      {error && (
        <p role="alert" className="studio-error">
          {error}
        </p>
      )}
      {report && (
        <>
          <p>
            <strong>Recognized:</strong> {report.recognized_text || '(no speech recognized)'}
          </p>
          <p className="studio-note">
            {report.words.length} recognized word timings · {report.recognizer.backend}{' '}
            {report.recognizer.model_size}. Timings describe recognized words, not forced alignment
            or proof that every script word was spoken.
          </p>
          {!report.differences.length && (
            <p>No normalized text differences found. Listen before accepting.</p>
          )}
          {report.differences.map((difference, index) => (
            <div
              key={`${difference.kind}:${difference.expected_start}:${index}`}
              className="studio-advanced"
            >
              <p>
                <strong>Possible {difference.kind}</strong> · Script:{' '}
                {difference.expected || '(none)'} · Recognized: {difference.recognized || '(none)'}
              </p>
              {difference.audition && (
                <StudioButton
                  disabled={studio.actionBusy || !asset}
                  onClick={() =>
                    studio.playback.audition(
                      client.assetAudio(asset!.id),
                      'Speech review interval',
                      1,
                      difference.audition!,
                    )
                  }
                >
                  Listen to suspected interval
                </StudioButton>
              )}
              {!!difference.expected && (
                <StudioButton
                  disabled={disabled || passage.locked}
                  onClick={() => onRepair(take!.id, difference)}
                >
                  Prepare replacement phrase
                </StudioButton>
              )}
            </div>
          ))}
        </>
      )}
      {!report && (project.document.speech_reviews?.length ?? 0) > 0 && (
        <p className="studio-note">
          Earlier reports do not match this take and script. Check again before preparing repairs.
        </p>
      )}
    </section>
  );
}
