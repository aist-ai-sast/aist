import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { fetchJson, normalizeList } from "./api";
import { getRoute } from "./routes";

export type ApiTokenScope = "read_only" | "read_write";

export type ApiToken = {
  id: number;
  name: string;
  scope: ApiTokenScope;
  organization_id: number;
  organization_name: string;
  last4: string;
  created: string;
  last_used_at: string | null;
  expires_at: string | null;
};

export type CreatedApiToken = ApiToken & { token: string };

export type CreateTokenPayload = {
  name: string;
  scope: ApiTokenScope;
  organization_id: number;
  expires_at?: string | null;
};

const TOKENS_KEY = ["my-api-tokens"];

export function useMyTokens() {
  return useQuery({
    queryKey: TOKENS_KEY,
    queryFn: async () => {
      const payload = await fetchJson<ApiToken[] | { results?: ApiToken[] }>(getRoute("me_tokens_url"));
      return normalizeList(payload);
    },
  });
}

export function useCreateToken() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: CreateTokenPayload) =>
      fetchJson<CreatedApiToken>(getRoute("me_tokens_url"), {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: TOKENS_KEY }),
  });
}

export function useDeleteToken() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (tokenId: number) =>
      fetchJson<void>(getRoute("me_token_detail_url", { token_id: tokenId }), { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: TOKENS_KEY }),
  });
}
