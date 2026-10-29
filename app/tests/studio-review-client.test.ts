import { expect, test } from 'bun:test';
import { ReviewClient } from '../src/features/studio/review-client';

const saved = { revision_id: 'r', version: 1, note: 'Saved', checklist: [], updated_at: 'now' };
const body = { operation_id: 'save', expected_version: 0, note: 'Saved', checklist: [] };
test('acknowledges exact ledger replay regardless of object field order', async () => {
  const request = { ...body, checklist: [{ id: 'pacing', label: 'Pacing', checked: true }] };
  // SQLite's encode(sort_keys=True) ledger emits this order on exact replay.
  const replay = { ...saved, checklist: [{ checked: true, id: 'pacing', label: 'Pacing' }] };
  const client = new ReviewClient('http://server', (async () =>
    Response.json({ review: replay })) as typeof fetch);
  expect((await client.save('p', 'r', request)).review.checklist).toEqual(request.checklist);
  await expect(
    client.save('p', 'r', {
      ...request,
      checklist: [{ ...request.checklist[0], checked: false }],
    }),
  ).rejects.toThrow('does not match');
});
test('review client encodes checkpoint identity and sends exact save/approval bodies', async () => {
  const calls: { url: string; body: string | undefined }[] = [];
  const client = new ReviewClient('http://server///', (async (url, init) => {
    calls.push({ url: String(url), body: init?.body as string | undefined });
    return Response.json({ review: { ...saved, revision_id: 'r/a' } });
  }) as typeof fetch);
  await client.save('p/a', 'r/a', body);
  await client.approve('p/a', 'r/a', { operation_id: 'approve', expected_version: 1 });
  expect(calls[0]).toEqual({
    url: 'http://server/studio/v1/projects/p%2Fa/reviews/r%2Fa',
    body: JSON.stringify(body),
  });
  expect(calls[1].url).toEndWith('/r%2Fa/approvals');
});
test('malformed or mismatched acknowledgements never clear recovery as saved', async () => {
  for (const review of [
    { ...saved, note: null },
    { ...saved, checklist: [{ id: 'x', label: 'X', checked: 'yes' }] },
    { ...saved, revision_id: 'foreign' },
    { ...saved, version: -1 },
    { ...saved, note: 'Not submitted' },
    { ...saved, version: 2 },
  ]) {
    const client = new ReviewClient('http://server', (async () =>
      Response.json({ review })) as typeof fetch);
    await expect(client.save('p', 'r', body)).rejects.toThrow();
  }
});
test('review conflicts retain HTTP status and current version for session recovery', async () => {
  const client = new ReviewClient('http://server', (async () =>
    Response.json(
      { detail: { message: 'Review changed', current_revision: 4 } },
      { status: 409 },
    )) as typeof fetch);
  try {
    await client.save('p', 'r', body);
    throw new Error('Expected conflict');
  } catch (error) {
    expect(error).toMatchObject({ status: 409, currentRevision: 4, message: 'Review changed' });
  }
});

test('review approval requests compact project projection without changing replay identity', async () => {
  let headers = new Headers();
  let submitted: unknown;
  const client = new ReviewClient('http://server', (async (_url, options) => {
    headers = new Headers(options?.headers);
    submitted = JSON.parse(String(options?.body));
    return Response.json({ review: saved });
  }) as typeof fetch);
  const request = { operation_id: 'approve-exactly', expected_version: 1 };
  await client.approve('p', 'r', request);
  expect(headers.get('X-Studio-Projection')).toBe('compact');
  expect(submitted).toEqual(request);
});
