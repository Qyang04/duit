"use client";

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
} from "@tanstack/react-query";

import {
  api,
  unwrap,
  type Account,
  type Category,
  type Transaction,
  type TransactionPage,
  type User,
} from "@/api/client";

export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: async () => unwrap<User>(await api.GET("/api/auth/me")),
  });
}

export function useCategories() {
  return useQuery({
    queryKey: ["categories"],
    queryFn: async () => unwrap<Category[]>(await api.GET("/api/categories")),
    staleTime: 5 * 60_000,
  });
}

export function useAccounts() {
  return useQuery({
    queryKey: ["accounts"],
    queryFn: async () => unwrap<Account[]>(await api.GET("/api/accounts")),
    staleTime: 5 * 60_000,
  });
}

export type Filters = {
  q: string;
  accountId: string; // "" = all
  category: string; // "" = all, "none" = uncategorised, else a category id
  dateFrom: string;
  dateTo: string;
};

export const NO_FILTERS: Filters = { q: "", accountId: "", category: "", dateFrom: "", dateTo: "" };

/** The ledger, newest first, one cursor page (50 rows) at a time. */
export function useTransactions(filters: Filters) {
  return useInfiniteQuery({
    queryKey: ["transactions", filters],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap<TransactionPage>(
        await api.GET("/api/transactions", {
          params: {
            query: {
              limit: 50,
              cursor: pageParam,
              q: filters.q || undefined,
              account_id: filters.accountId || undefined,
              category_id:
                filters.category && filters.category !== "none" ? filters.category : undefined,
              uncategorised: filters.category === "none" || undefined,
              date_from: filters.dateFrom || undefined,
              date_to: filters.dateTo || undefined,
            },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

type Pages = InfiniteData<TransactionPage, string | undefined>;

/** Stamp a transaction with a category. Optimistic: every cached list shows the new stamp at
 *  once; if the API refuses, the previous lists are put back. */
export function useSetCategory() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, categoryId }: { id: string; categoryId: string | null }) =>
      unwrap<Transaction>(
        await api.PATCH("/api/transactions/{txn_id}", {
          params: { path: { txn_id: id } },
          body: { category_id: categoryId },
        }),
      ),
    onMutate: async ({ id, categoryId }) => {
      await qc.cancelQueries({ queryKey: ["transactions"] });
      const previous = qc.getQueriesData<Pages>({ queryKey: ["transactions"] });
      qc.setQueriesData<Pages>({ queryKey: ["transactions"] }, (data) =>
        data
          ? {
              ...data,
              pages: data.pages.map((page) => ({
                ...page,
                items: page.items.map((t) => (t.id === id ? { ...t, category_id: categoryId } : t)),
              })),
            }
          : data,
      );
      return { previous };
    },
    onError: (_error, _vars, context) => {
      context?.previous.forEach(([key, data]) => qc.setQueryData(key, data));
    },
    // Filtered views (e.g. "uncategorised") refresh in the background afterwards.
    onSettled: () => qc.invalidateQueries({ queryKey: ["transactions"], refetchType: "inactive" }),
  });
}
