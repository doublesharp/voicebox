import { CheckCircle2, History } from 'lucide-react';
import { useState } from 'react';
import { Textarea } from '@/components/ui/textarea';
import { useStudio } from './context';
import { Field, StudioButton } from './ui';

export function Review() {
  const studio = useStudio();
  const { project, session, revisionId, setRevisionId } = studio;
  const [note, setNote] = useState('');
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
          <Field label="Local review note">
            <Textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Pacing, pronunciation, and timing checked…"
            />
          </Field>
          <div className="studio-action-row">
            <StudioButton
              primary
              disabled={!studio.canMutate}
              onClick={() =>
                void session.approve(revision.id, note).then((ok) => {
                  if (ok) setNote('');
                })
              }
            >
              <CheckCircle2 size={15} /> Approve this checkpoint locally
            </StudioButton>
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
