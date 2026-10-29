import { afterEach, expect, test } from 'bun:test';
import { onlineManager, QueryObserver } from '@tanstack/react-query';
import { queryClient } from '../src/lib/queryClient';

afterEach(() => {
  onlineManager.setOnline(true);
  queryClient.clear();
});

test('queries reach the backend while the browser reports offline', async () => {
  onlineManager.setOnline(false);
  let calls = 0;
  const result = queryClient.fetchQuery({ queryKey: ['offline'], queryFn: async () => ++calls });
  void result.catch(() => {});
  await new Promise((resolve) => setTimeout(resolve, 10));
  expect(calls).toBe(1);
  expect(await result).toBe(1);
});

test('generation mutations reach the backend while offline', async () => {
  onlineManager.setOnline(false);
  let calls = 0;
  const mutation = queryClient
    .getMutationCache()
    .build(queryClient, { mutationFn: async () => ++calls });
  const result = mutation.execute(undefined);
  void result.catch(() => {});
  await new Promise((resolve) => setTimeout(resolve, 10));
  expect(calls).toBe(1);
  expect(await result).toBe(1);
});

test('a failed remote-server query refreshes when connectivity returns', async () => {
  onlineManager.setOnline(false);
  queryClient.mount();
  let calls = 0;
  const observer = new QueryObserver(queryClient, {
    queryKey: ['remote-offline'],
    retry: false,
    queryFn: async () => {
      calls++;
      if (!onlineManager.isOnline()) throw new Error('Network unavailable');
      return 'reconnected';
    },
  });
  const unsubscribe = observer.subscribe(() => {});
  try {
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(observer.getCurrentResult().status).toBe('error');
    onlineManager.setOnline(true);
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(observer.getCurrentResult().data).toBe('reconnected');
    expect(calls).toBe(2);
  } finally {
    unsubscribe();
    queryClient.unmount();
  }
});
