import { Link, useNavigate } from '@tanstack/react-router';
import { ArrowRight, FileArchive, FolderOpen, Plus, Search, Upload } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { useServerStore } from '@/stores/serverStore';
import { type LegacyStory, StudioClient } from './client';
import { splitScript } from './domain';
import { errorMessage } from './session';
import type { StudioProject, StudioProjectSummary } from './types';
import { EmptyState, Field, StudioButton } from './ui';
import './studio.css';

export function ProjectsPage() {
  const serverUrl = useServerStore((state) => state.serverUrl);
  const client = useMemo(() => new StudioClient(serverUrl), [serverUrl]);
  const navigate = useNavigate();
  const [projects, setProjects] = useState<StudioProjectSummary[]>([]);
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [storyOpen, setStoryOpen] = useState(false);
  const [stories, setStories] = useState<LegacyStory[]>([]);
  const [storiesLoading, setStoriesLoading] = useState(false);
  const [storyId, setStoryId] = useState('');
  const [name, setName] = useState('');
  const [label, setLabel] = useState('');
  const [script, setScript] = useState('');
  const [language, setLanguage] = useState('en');
  const archiveInput = useRef<HTMLInputElement>(null);
  const epoch = useRef(0);
  const busyRef = useRef(false);
  const activeClient = useRef(client);
  useEffect(() => {
    activeClient.current = client;
    epoch.current++;
    busyRef.current = false;
    setBusy(false);
    return () => {
      epoch.current++;
    };
  }, [client]);
  const load = useCallback(
    async (signal?: AbortSignal) => {
      const token = epoch.current;
      setLoading(true);
      setError(null);
      try {
        const response = await client.projects(signal);
        if (!signal?.aborted && token === epoch.current && activeClient.current === client)
          setProjects(response.items);
      } catch (error) {
        if (!signal?.aborted && token === epoch.current && activeClient.current === client)
          setError(errorMessage(error));
      } finally {
        if (!signal?.aborted && token === epoch.current && activeClient.current === client)
          setLoading(false);
      }
    },
    [client],
  );
  useEffect(() => {
    const request = new AbortController();
    void load(request.signal);
    return () => request.abort();
  }, [load]);
  useEffect(() => {
    if (!storyOpen) return;
    const request = new AbortController();
    setStoriesLoading(true);
    setError(null);
    client
      .stories(request.signal)
      .then((items) => {
        if (!request.signal.aborted) setStories(items);
      })
      .catch((error) => {
        if (!request.signal.aborted) setError(errorMessage(error));
      })
      .finally(() => {
        if (!request.signal.aborted) setStoriesLoading(false);
      });
    return () => request.abort();
  }, [client, storyOpen]);
  const openProject = async (action: () => Promise<StudioProject>) => {
    if (busyRef.current) return;
    busyRef.current = true;
    const token = epoch.current;
    setBusy(true);
    setError(null);
    try {
      const project = await action();
      if (token !== epoch.current) return;
      await navigate({
        to: '/studio/projects/$projectId/$workspace',
        params: { projectId: project.id, workspace: 'edit' },
      });
    } catch (error) {
      if (token === epoch.current) setError(errorMessage(error));
    } finally {
      if (token === epoch.current) {
        busyRef.current = false;
        setBusy(false);
      }
    }
  };
  const filtered = projects.filter((p) =>
    `${p.name} ${p.client_label ?? ''}`.toLowerCase().includes(search.toLowerCase()),
  );
  const paragraphs = splitScript(script);
  return (
    <section className="studio studio-projects" aria-label="Studio projects">
      <header className="studio-landing-header">
        <div>
          <p className="studio-eyebrow">VOICEBOX DS / STUDIO</p>
          <h1>Your next great take starts here.</h1>
          <p>Script, performance, and delivery. Everything stays with the project.</p>
        </div>
        <Dialog open={createOpen} onOpenChange={setCreateOpen}>
          <DialogTrigger asChild>
            <button className="studio-primary" type="button">
              <Plus size={16} /> New project
            </button>
          </DialogTrigger>
          <DialogContent className="studio studio-modal">
            <DialogHeader>
              <DialogTitle>Create a project</DialogTitle>
              <DialogDescription>
                Paste a script or begin with an empty canvas. Blank lines create separate passages.
              </DialogDescription>
            </DialogHeader>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void openProject(() =>
                  client.createProject({
                    name: name.trim(),
                    client_label: label.trim() || undefined,
                    script,
                    language,
                  }),
                );
              }}
            >
              <Field label="Project name">
                <Input
                  autoFocus
                  required
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="A story worth telling"
                  maxLength={200}
                />
              </Field>
              <div className="studio-field-grid">
                <Field label="Client / collection">
                  <Input
                    value={label}
                    onChange={(e) => setLabel(e.target.value)}
                    placeholder="Optional"
                  />
                </Field>
                <Field label="Language code">
                  <Input
                    required
                    value={language}
                    onChange={(e) => setLanguage(e.target.value)}
                    placeholder="en"
                  />
                </Field>
              </div>
              <Field label="Script">
                <Textarea
                  value={script}
                  onChange={(e) => setScript(e.target.value)}
                  rows={6}
                  placeholder="Paste your script here…"
                />
              </Field>
              <details className="studio-paste-preview">
                <summary>
                  {paragraphs.length} {paragraphs.length === 1 ? 'passage' : 'passages'} in your
                  script
                </summary>
                <ol>
                  {paragraphs.map((text, index) => (
                    <li key={`${index}-${text.slice(0, 20)}`}>{text}</li>
                  ))}
                </ol>
              </details>
              {error && (
                <p role="alert" className="studio-error">
                  {error}
                </p>
              )}
              <button
                className="studio-primary"
                type="submit"
                disabled={busy || !name.trim() || !language.trim()}
              >
                {busy ? 'Creating…' : 'Create project'}
                <ArrowRight size={15} />
              </button>
            </form>
          </DialogContent>
        </Dialog>
      </header>
      <div className="studio-project-toolbar">
        <div className="studio-search">
          <Search size={16} aria-hidden="true" />
          <Input
            aria-label="Search projects"
            placeholder="Search projects or clients"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <StudioButton onClick={() => archiveInput.current?.click()} disabled={busy}>
          <FileArchive size={15} /> Import archive
        </StudioButton>
        <Dialog open={storyOpen} onOpenChange={setStoryOpen}>
          <DialogTrigger asChild>
            <button type="button" className="studio-quiet">
              <Upload size={15} /> Import Story
            </button>
          </DialogTrigger>
          <DialogContent className="studio studio-modal">
            <DialogHeader>
              <DialogTitle>Create a studio copy</DialogTitle>
              <DialogDescription>
                The original Story stays intact. Its audio and arrangement become owned project
                assets.
              </DialogDescription>
            </DialogHeader>
            <Field label="Story">
              <select
                value={storyId}
                onChange={(event) => setStoryId(event.target.value)}
                disabled={storiesLoading || busy}
              >
                <option value="">{storiesLoading ? 'Loading Stories…' : 'Choose a Story'}</option>
                {stories.map((story) => (
                  <option key={story.id} value={story.id}>
                    {story.name}
                  </option>
                ))}
              </select>
            </Field>
            {!storiesLoading && !stories.length && (
              <p>No Stories yet. Existing Stories remain available from the sidebar.</p>
            )}
            {error && (
              <p role="alert" className="studio-error">
                {error}
              </p>
            )}
            <StudioButton
              primary
              disabled={!storyId || busy}
              onClick={() => void openProject(() => client.importStory(storyId))}
            >
              {busy ? 'Importing…' : 'Create studio copy'}
            </StudioButton>
          </DialogContent>
        </Dialog>
        <input
          ref={archiveInput}
          type="file"
          accept=".zip,application/zip"
          className="sr-only"
          aria-label="Import studio archive"
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = '';
            if (file) void openProject(() => client.importArchive(file));
          }}
        />
      </div>
      {error && !createOpen && !storyOpen && (
        <div className="studio-banner" role="alert">
          <span>{error}</span>
          <StudioButton onClick={() => void load()}>Retry loading</StudioButton>
        </div>
      )}
      {busy && <p aria-live="polite">Importing your project and validating audio…</p>}
      <div className="studio-section-title">
        <h2>Projects</h2>
        <span>
          {filtered.length} {filtered.length === 1 ? 'project' : 'projects'}
        </span>
      </div>
      {loading ? (
        <p aria-live="polite" className="studio-loading">
          Opening your studio…
        </p>
      ) : filtered.length ? (
        <div className="studio-project-grid">
          {filtered.map((project) => (
            <Link
              key={project.id}
              to="/studio/projects/$projectId/$workspace"
              params={{ projectId: project.id, workspace: 'edit' }}
              className="studio-project-card"
            >
              <div className="studio-project-art">
                <FolderOpen size={30} strokeWidth={1.3} />
                <span>DS</span>
              </div>
              <div>
                <span className="studio-eyebrow">{project.client_label || 'PERSONAL PROJECT'}</span>
                <h3>{project.name}</h3>
                <p>
                  Draft {project.revision} <span aria-hidden="true">·</span>{' '}
                  {new Date(project.updated_at).toLocaleDateString()}
                </p>
              </div>
              <ArrowRight size={16} className="studio-card-arrow" />
            </Link>
          ))}
        </div>
      ) : (
        <EmptyState
          title={search ? 'No matching projects' : 'Make room for your next idea'}
          action={
            !search && (
              <StudioButton primary onClick={() => setCreateOpen(true)}>
                <Plus size={15} /> Create your first project
              </StudioButton>
            )
          }
        >
          {search
            ? 'Try another title or client label.'
            : 'Start with a script, import a Story, or reopen a complete project archive.'}
        </EmptyState>
      )}
      <footer className="studio-landing-footer">
        <span>Local projects. Lasting performances.</span>
        <span>DoubleSharp Edition</span>
      </footer>
    </section>
  );
}
