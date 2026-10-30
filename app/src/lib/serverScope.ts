import { useServerStore } from '@/stores/serverStore';

export interface ServerScope {
  serverUrl: string;
  serverRevision: number;
}

export function currentServerScope(): ServerScope {
  const { serverUrl, serverRevision } = useServerStore.getState();
  return { serverUrl, serverRevision };
}

export function isCurrentServer(scope: ServerScope): boolean {
  const current = useServerStore.getState();
  return current.serverUrl === scope.serverUrl && current.serverRevision === scope.serverRevision;
}

export function requireCurrentServer(scope?: ServerScope): void {
  if (scope && !isCurrentServer(scope)) throw new Error('Server connection changed');
}
