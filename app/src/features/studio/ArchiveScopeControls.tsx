export interface ArchiveScope {
  take_scope: 'all' | 'used';
  include_regeneration_references: boolean;
}

export const completeArchiveScope: ArchiveScope = {
  take_scope: 'all',
  include_regeneration_references: true,
};

export function archiveScopeQuery(scope: ArchiveScope) {
  return new URLSearchParams({
    take_scope: scope.take_scope,
    include_regeneration_references: String(scope.include_regeneration_references),
  }).toString();
}

export function hasMissingRegenerationReferences(voice: Record<string, unknown> | null) {
  return Array.isArray(voice?.unavailable_references) && voice.unavailable_references.length > 0;
}

export function RegenerationReferenceNotice() {
  return (
    <p className="studio-error" role="alert">
      Voice reference audio is unavailable for a selected passage. Existing recordings still play.
      Reassign its voice with reference audio before generating again.
    </p>
  );
}

export function ArchiveScopeControls({
  value,
  onChange,
  disabled = false,
}: {
  value: ArchiveScope;
  onChange: (value: ArchiveScope) => void;
  disabled?: boolean;
}) {
  return (
    <fieldset disabled={disabled} className="studio-field">
      <legend>Project archive contents</legend>
      <label>
        Recordings
        <select
          aria-label="Archived recordings"
          value={value.take_scope}
          onChange={(event) =>
            onChange({ ...value, take_scope: event.target.value as ArchiveScope['take_scope'] })
          }
        >
          <option value="all">All available takes, including alternatives</option>
          <option value="used">Takes used by the draft and saved checkpoints</option>
        </select>
      </label>
      <label className="studio-check">
        <input
          type="checkbox"
          checked={value.include_regeneration_references}
          onChange={(event) =>
            onChange({ ...value, include_regeneration_references: event.target.checked })
          }
        />{' '}
        Include voice reference audio for regeneration
      </label>
      <p className="studio-note">
        Saved checkpoints, approvals and review notes stay included. Model weights, caches and undo
        history are excluded. Recorded jobs are provenance only. Save any pending review notes
        before exporting; recovery journals are kept separately.
      </p>
      {!value.include_regeneration_references && (
        <p className="studio-note">
          Included recordings will still play. Reassign a voice with reference audio to generate new
          takes after reopening. Recorded models may need to be installed separately.
        </p>
      )}
    </fieldset>
  );
}
