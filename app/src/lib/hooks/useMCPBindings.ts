import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiClient } from '@/lib/api/client';
import type { MCPClientBindingUpsert } from '@/lib/api/types';

const MCP_BINDINGS_KEY = ['settings', 'mcp', 'bindings'] as const;

/** Manage per-MCP-client voice bindings (Claude Code → Morgan, etc.). */
export function useMCPBindings() {
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: MCP_BINDINGS_KEY,
    queryFn: () => apiClient.listMCPBindings(),
    // Keep fresh while the Settings page is open — the ``last_seen_at``
    // timestamp is useful for confirming an install works, and we want it
    // to tick forward when a client connects.
    refetchInterval: 10_000,
  });

  const upsertMutation = useMutation({
    mutationFn: (data: MCPClientBindingUpsert) =>
      apiClient.upsertMCPBinding(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: MCP_BINDINGS_KEY });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (clientId: string) => apiClient.deleteMCPBinding(clientId),
    // Keep the confirmed list visible until the server accepts the deletion.
    // Restoring a whole snapshot on failure would overwrite concurrent edits.
    onSuccess: () => queryClient.invalidateQueries({ queryKey: MCP_BINDINGS_KEY }),
  });

  return {
    bindings: query.data?.items ?? [],
    isLoading: query.isLoading,
    upsert: upsertMutation.mutate,
    upsertAsync: upsertMutation.mutateAsync,
    remove: deleteMutation.mutate,
    removeAsync: deleteMutation.mutateAsync,
    isRemoving: deleteMutation.isPending,
  };
}
