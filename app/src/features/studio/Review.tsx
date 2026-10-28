import { CheckCircle2, History } from 'lucide-react';
import { useState, useSyncExternalStore } from 'react';
import { Textarea } from '@/components/ui/textarea';
import { useStudio } from './context';
import type { ReviewSession } from './review-session';
import { Field, StudioButton } from './ui';

export function Review() {
  const studio = useStudio();
  const { project, session, revisionId, setRevisionId } = studio;
  const [restoreConfirm, setRestoreConfirm] = useState(false);
  const revision = project.revisions.find((r) => r.id === revisionId);
  const approvals = project.approvals.filter((a) => a.revision_id === revision?.id);
  const deliverables = project.deliverables.filter((d) => d.revision_id === revision?.id);
  return (
    <section className="studio-review" aria-label="Local revision review">
      <p className="studio-eyebrow">A MOMENT WORTH KEEPING</p>
      <h2>Review with confidence.</h2>
      <p className="studio-note">
        Review an immutable checkpoint. Local approval stays with this exact revision when the draft
        changes.
      </p>
      <Field label="Review checkpoint">
        <select
          value={revisionId}
          onChange={(e) => {
            setRevisionId(e.target.value);
            setRestoreConfirm(false);
          }}
        >
          <option value="">Choose a checkpoint</option>
          {project.revisions.map((r) => (
            <option key={r.id} value={r.id}>
              {r.name} · draft {r.project_revision}
            </option>
          ))}
        </select>
      </Field>
      {!revision ? (
        <div className="studio-review-empty">
          Create a named checkpoint in the project header to start your review.
        </div>
      ) : (
        <>
          <div className="studio-review-banner">
            <History size={20} />
            <div>
              <strong>{revision.name}</strong>
              <span>
                Frozen draft {revision.project_revision} ·{' '}
                {new Date(revision.created_at).toLocaleString()}
              </span>
            </div>
            <span className="studio-status-chip">
              {approvals.length ? 'Locally approved' : 'Awaiting review'}
            </span>
          </div>
          <ol className="studio-review-script">
            {revision.document.passages.map((passage, index) => (
              <li key={passage.id}>
                <span className="studio-mono">{String(index + 1).padStart(2, '0')}</span>
                <p>{passage.text}</p>
                <span className="studio-note">
                  {passage.selected_take_id ? 'Pinned take' : 'No selected take'}
                </span>
              </li>
            ))}
          </ol>
          <div className="studio-review-deliverables">
            <h3>Rendered copies of this checkpoint</h3>
            {!deliverables.length && (
              <p className="studio-note">
                Use Deliver to render this checkpoint for an actual-file review.
              </p>
            )}
            {deliverables.map((file) => (
              <StudioButton
                key={file.id}
                onClick={() =>
                  void studio.playback.audition(
                    studio.client.deliverableAudio(file.id),
                    file.filename || `${file.kind}.${file.format}`,
                  )
                }
              >
                Audition {file.filename || `${file.kind}.${file.format}`}
              </StudioButton>
            ))}
          </div>
          {studio.reviewSessions ? (
            <ReviewEditor key={revision.id} review={studio.reviewSessions.get(revision.id)} />
          ) : (
            <p role="alert">Local review storage is unavailable.</p>
          )}
          <div className="studio-action-row">
            <StudioButton
              disabled={!studio.canMutate}
              onClick={() => setRestoreConfirm(!restoreConfirm)}
            >
              Restore as a new draft
            </StudioButton>
          </div>
          {restoreConfirm && (
            <div className="studio-banner">
              <p>
                Restore “{revision.name}” as a new draft? Existing checkpoints and approvals stay
                unchanged.
              </p>
              <StudioButton
                onClick={() =>
                  void session
                    .command('restore_revision', { revision_id: revision.id })
                    .then((ok) => {
                      if (ok) setRestoreConfirm(false);
                    })
                }
                disabled={!studio.canMutate}
              >
                Confirm restore
              </StudioButton>
              <StudioButton onClick={() => setRestoreConfirm(false)}>Cancel</StudioButton>
            </div>
          )}
          <div className="studio-approval-list">
            {approvals.map((approval) => (
              <div key={approval.id}>
                <CheckCircle2 size={15} />
                <p>
                  <strong>Local approval</strong>
                  <span>{approval.note || 'No note added.'}</span>
                  <small>{new Date(approval.created_at).toLocaleString()}</small>
                </p>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function ReviewEditor({ review }: { review: ReviewSession }) {
  const studio = useStudio();
  const state = useSyncExternalStore(review.subscribe, review.getSnapshot);
  const [showCopy, setShowCopy] = useState(false);
  const value = review.value();
  const blocked = !state.review || ['loading', 'recovery'].includes(state.status);
  const labels = {
    loading: 'Loading review…',
    saved: 'Review saved',
    dirty: 'Review has unsaved changes',
    saving: 'Saving review…',
    error: 'Review save failed',
    recovery: 'Review recovery available',
    conflict: 'Review save conflict',
  };
  return (
    <section aria-label="Checkpoint review notes and checklist">
      <output aria-live="polite">{labels[state.status]}</output>
      {state.error && <p role="alert">{state.error}</p>}
      {!state.recoverySaved && (
        <p role="alert">
          Local recovery storage is unavailable. Keep this project open and copy your review before
          leaving.
        </p>
      )}
      <Field label="Local review note">
        <Textarea
          value={value.note}
          disabled={blocked}
          maxLength={4000}
          onChange={(e) => review.edit({ note: e.target.value })}
          placeholder="Pacing, pronunciation, and timing checked…"
        />
      </Field>
      <fieldset disabled={blocked}>
        <legend>Local review checklist (optional)</legend>
        {value.checklist.map((item) => (
          <label key={item.id}>
            <input
              type="checkbox"
              checked={item.checked}
              onChange={(e) =>
                review.edit({
                  checklist: value.checklist.map((i) =>
                    i.id === item.id ? { ...i, checked: e.target.checked } : i,
                  ),
                })
              }
            />
            {item.label}
          </label>
        ))}
      </fieldset>
      <div className="studio-action-row">
        <StudioButton
          disabled={blocked || ['error', 'conflict'].includes(state.status)}
          onClick={() => review.flush()}
        >
          Save review
        </StudioButton>
        <StudioButton
          primary
          disabled={
            !studio.canMutate ||
            !state.review ||
            ['loading', 'error', 'conflict', 'recovery', 'saving'].includes(state.status)
          }
          onClick={async () => {
            if (await review.approve()) await studio.session.refresh();
          }}
        >
          <CheckCircle2 size={15} /> Approve this checkpoint locally
        </StudioButton>
        {state.status === 'error' && (
          <StudioButton onClick={() => review.retry()}>Retry exact review request</StudioButton>
        )}
        {(!state.recoverySaved || ['error', 'conflict', 'recovery'].includes(state.status)) && (
          <>
            <StudioButton onClick={() => setShowCopy(!showCopy)}>
              Show review recovery copy
            </StudioButton>
            <StudioButton
              onClick={() => {
                if (
                  window.confirm(
                    'Discard the current review draft and reload the saved review? Other recovery copies stay available. Copy your draft first if needed.',
                  )
                )
                  void review.reloadSaved();
              }}
            >
              Discard current draft and reload review
            </StudioButton>
          </>
        )}
        {state.recoveries.map((record, index) => (
          <div key={record.key}>
            <StudioButton
              disabled={!record.readable || !!state.draft || state.status === 'saving'}
              onClick={() => review.recover(record.key)}
            >
              Recover review copy {index + 1}
            </StudioButton>
            <StudioButton
              onClick={() => {
                if (
                  window.confirm(
                    `Discard review recovery copy ${index + 1}? Copy it first if needed.`,
                  )
                )
                  review.discardRecovery(record.key);
              }}
            >
              Discard review copy {index + 1}
            </StudioButton>
          </div>
        ))}
      </div>
      {showCopy && (
        <Field label="Review recovery copy">
          <Textarea readOnly value={review.recoveryCopy()} />
        </Field>
      )}
      <p className="studio-note">
        Approval records the saved note and actual checklist state for this checkpoint. This is
        local self-approval.
      </p>
    </section>
  );
}
