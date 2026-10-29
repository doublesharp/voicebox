import { afterEach, expect, test } from 'bun:test';
import { createElement } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { StudioClient } from '../src/features/studio/client';
import type { StudioRevision } from '../src/features/studio/types';
import { useRevisionDocument } from '../src/features/studio/useRevisionDocument';

const mounted: ReactTestRenderer[] = [];
afterEach(async () => {
  await act(async () => {
    for (const renderer of mounted.splice(0)) renderer.unmount();
  });
});

test('closed delivery makes no checkpoint request and late checkpoint replies cannot replace the new selection', async () => {
  const requests: Array<{ url: string; signal: AbortSignal; resolve: (value: Response) => void }> =
    [];
  const client = new StudioClient(
    'http://local',
    ((url: string, options: RequestInit) =>
      new Promise<Response>((resolve) =>
        requests.push({ url, signal: options.signal!, resolve }),
      )) as typeof fetch,
  );
  const revision = (id: string) =>
    ({ id, name: id, project_revision: 0, created_at: '' }) as StudioRevision;
  function Probe({ selected, enabled }: { selected: StudioRevision; enabled: boolean }) {
    const result = useRevisionDocument(client, 'project', selected, enabled);
    return createElement('output', null, result.document?.language ?? result.error ?? 'loading');
  }
  let view!: ReactTestRenderer;
  await act(async () => {
    view = create(createElement(Probe, { selected: revision('first'), enabled: false }));
    mounted.push(view);
  });
  expect(requests).toHaveLength(0);
  await act(async () =>
    view.update(createElement(Probe, { selected: revision('first'), enabled: true })),
  );
  expect(requests[0].url).toEndWith('/projects/project/revisions/first');
  await act(async () =>
    view.update(createElement(Probe, { selected: revision('second'), enabled: true })),
  );
  expect(requests[0].signal.aborted).toBe(true);
  await act(async () => requests[1].resolve(Response.json({ document: { language: 'second' } })));
  expect(view.root.findByType('output').children).toEqual(['second']);
  await act(async () => requests[0].resolve(Response.json({ document: { language: 'first' } })));
  expect(view.root.findByType('output').children).toEqual(['second']);
});

test('conditional project transport handles 304 without reading a JSON body', async () => {
  const requests: Array<{ url: string; options: RequestInit }> = [];
  const client = new StudioClient('http://local', (async (url: string, options: RequestInit) => {
    requests.push({ url, options });
    return new Response(null, { status: 304 });
  }) as typeof fetch);
  expect(await client.refreshProject('project', '"snapshot"')).toBe(null);
  expect(requests[0].options.headers).toEqual({ 'If-None-Match': '"snapshot"' });
  expect(requests[0].url).toEndWith('/projects/project?compact=true');
});
