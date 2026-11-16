"use client";

import { AnimatePresence, motion } from "motion/react";
import { useEffect, useMemo, useRef, useState } from "react";

import type { Account, Category, Transaction } from "@/api/client";
import { Shell } from "@/components/Shell";
import { StampPicker, type PickerTarget } from "@/components/StampPicker";
import { formatSen, parts, signOf, tiltFor } from "@/lib/money";
import {
  NO_FILTERS,
  useAccounts,
  useCategories,
  useSetCategory,
  useTransactions,
  type Filters,
} from "@/lib/queries";

export default function LedgerPage() {
  return (
    <Shell>
      <Ledger />
    </Shell>
  );
}

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = window.setTimeout(() => setV(value), ms);
    return () => window.clearTimeout(t);
  }, [value, ms]);
  return v;
}

function Ledger() {
  const [draft, setDraft] = useState<Filters>(NO_FILTERS);
  const q = useDebounced(draft.q, 250);
  const filters = useMemo(() => ({ ...draft, q }), [draft, q]);

  const transactions = useTransactions(filters);
  const categories = useCategories();
  const accounts = useAccounts();
  const setCategory = useSetCategory();

  const rows = useMemo(
    () => transactions.data?.pages.flatMap((p) => p.items) ?? [],
    [transactions.data],
  );
  const byId = useMemo(
    () => new Map((categories.data ?? []).map((c) => [c.id, c])),
    [categories.data],
  );

  const [focused, setFocused] = useState(-1);
  const [picker, setPicker] = useState<PickerTarget | null>(null);
  const [justStamped, setJustStamped] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const rowRefs = useRef(new Map<string, HTMLElement>());

  function openPicker(t: Transaction) {
    // Two stamp buttons per row (phone and desktop layouts): anchor to the visible one.
    const el = [...(rowRefs.current.get(t.id)?.querySelectorAll<HTMLElement>("[data-stamp]") ?? [])]
      .find((b) => b.getClientRects().length > 0);
    if (el) setPicker({ txnId: t.id, current: t.category_id, rect: el.getBoundingClientRect() });
  }

  function stamp(txnId: string, categoryId: string | null) {
    setPicker(null);
    setJustStamped(categoryId ? txnId : null);
    setCategory.mutate(
      { id: txnId, categoryId },
      {
        onError: (e) => setToast(e.message || "Could not save the stamp"),
      },
    );
  }

  // Keyboard: "/" search, j/k move, c (or Enter) stamps the focused row, Esc leaves.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const el = e.target as HTMLElement;
      if (el.closest("input, select, textarea, [popover]") || e.metaKey || e.ctrlKey || e.altKey)
        return;
      if (e.key === "/") {
        e.preventDefault();
        searchRef.current?.focus();
      } else if (e.key === "j" || e.key === "k") {
        e.preventDefault();
        setFocused((i) => {
          const next = Math.max(0, Math.min(rows.length - 1, i + (e.key === "j" ? 1 : -1)));
          const t = rows[next];
          if (t) rowRefs.current.get(t.id)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
          return next;
        });
      } else if ((e.key === "c" || e.key === "Enter") && rows[focused]) {
        e.preventDefault();
        openPicker(rows[focused]);
      } else if (e.key === "Escape") setFocused(-1);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // Infinite scroll: load the next page when the bottom of the ledger comes into view.
  const sentinel = useRef<HTMLDivElement>(null);
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = transactions;
  useEffect(() => {
    const el = sentinel.current;
    if (!el) return;
    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry?.isIntersecting && hasNextPage && !isFetchingNextPage) void fetchNextPage();
      },
      { rootMargin: "400px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), 3500);
    return () => window.clearTimeout(t);
  }, [toast]);

  const filtered = JSON.stringify(draft) !== JSON.stringify(NO_FILTERS);
  const showAccount = (accounts.data?.length ?? 0) > 1;
  const accountName = new Map((accounts.data ?? []).map((a) => [a.id, a.name]));

  return (
    <main className="mx-auto max-w-5xl px-4 pt-10 sm:px-8 sm:pt-14">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="eyebrow">Buku akaun</p>
          <h1 className="mt-1 font-display text-5xl tracking-tight sm:text-6xl">Ledger</h1>
        </div>
        <p className="hidden items-center gap-1.5 text-xs text-ink-3 sm:flex">
          <kbd>/</kbd> search <kbd>j</kbd>
          <kbd>k</kbd> move <kbd>c</kbd> stamp
        </p>
      </header>

      <FilterSlip
        value={draft}
        onChange={setDraft}
        accounts={accounts.data ?? []}
        categories={categories.data ?? []}
        searchRef={searchRef}
        filtered={filtered}
      />

      <section className="ledger mt-2" aria-label="Transactions">
        {transactions.isPending ? (
          <p className="figures py-16 pl-24 text-sm text-ink-3">
            printing <span className="cursor" />
          </p>
        ) : rows.length === 0 ? (
          <EmptyPage filtered={filtered} onClear={() => setDraft(NO_FILTERS)} />
        ) : (
          <ol>
            {rows.map((t, i) => {
              const month = parts(t.posted_on);
              const newMonth = i === 0 || parts(rows[i - 1]!.posted_on).key !== month.key;
              // The first screenful "prints". A CSS animation runs once, when the row mounts,
              // so rows that stay on screen (e.g. after a stamp) never replay it.
              const print = i < 24;
              return (
                <li key={t.id}>
                  {newMonth && <MonthDivider label={month.long} />}
                  <Row
                    t={t}
                    index={i}
                    print={print}
                    focused={i === focused}
                    category={t.category_id ? byId.get(t.category_id) : undefined}
                    parent={
                      t.category_id ? byId.get(byId.get(t.category_id)?.parent_id ?? "") : undefined
                    }
                    account={showAccount ? accountName.get(t.account_id) : undefined}
                    animateStamp={justStamped === t.id}
                    rowRef={(el) => {
                      if (el) rowRefs.current.set(t.id, el);
                      else rowRefs.current.delete(t.id);
                    }}
                    onStamp={() => {
                      setFocused(i);
                      openPicker(t);
                    }}
                  />
                </li>
              );
            })}
          </ol>
        )}
        <div ref={sentinel} className="h-px" />
        {isFetchingNextPage && (
          <p className="figures py-6 pl-24 text-sm text-ink-3">
            printing more <span className="cursor" />
          </p>
        )}
        {!hasNextPage && rows.length > 0 && (
          <p className="py-10 text-center font-display text-ink-3">— end of entries —</p>
        )}
      </section>

      <StampPicker
        target={picker}
        categories={categories.data ?? []}
        onPick={stamp}
        onClose={() => setPicker(null)}
      />

      <AnimatePresence>
        {toast && (
          <motion.div
            role="status"
            className="figures fixed bottom-24 right-4 z-40 max-w-xs rounded-sm border border-chop/40 bg-paper px-4 py-3 text-sm text-chop shadow-lg md:bottom-6"
            initial={{ y: 30, opacity: 0, rotate: 2 }}
            animate={{ y: 0, opacity: 1, rotate: -1 }}
            exit={{ y: 30, opacity: 0 }}
          >
            {toast}
          </motion.div>
        )}
      </AnimatePresence>
    </main>
  );
}

function MonthDivider({ label }: { label: string }) {
  return (
    <div className="month-head sticky top-0 z-10 flex items-center gap-4 pb-2 pt-8">
      <h2 className="font-display text-2xl">{label}</h2>
      <span className="h-px flex-1 bg-ink/15" />
    </div>
  );
}

function Row({
  t,
  index,
  print,
  focused,
  category,
  parent,
  account,
  animateStamp,
  rowRef,
  onStamp,
}: {
  t: Transaction;
  index: number;
  print: boolean;
  focused: boolean;
  category?: Category;
  parent?: Category;
  account?: string;
  animateStamp: boolean;
  rowRef: (el: HTMLElement | null) => void;
  onStamp: () => void;
}) {
  const date = parts(t.posted_on);
  const [first = "", ...rest] = t.merchant_raw.split("\n");
  const credit = t.amount_sen > 0;
  const tilt = tiltFor(t.id);

  return (
    <article
      ref={rowRef}
      className="entry grid grid-cols-[3.6rem_1fr_auto] items-start gap-x-4 py-3.5 sm:grid-cols-[4.6rem_1fr_auto_9.5rem] sm:gap-x-6"
      data-print={print}
      data-focused={focused}
      style={{ ["--i" as string]: Math.min(index, 23) }}
    >
      <div className="figures pt-0.5 text-right leading-tight">
        <div className="text-lg font-medium">{date.day}</div>
        <div className="text-[0.65rem] tracking-[0.18em] text-ink-3">{date.month}</div>
      </div>

      <div className="min-w-0 pl-2 sm:pl-4">
        <p className="truncate font-medium">{first}</p>
        {rest.length > 0 && (
          <p className="mt-0.5 line-clamp-2 text-sm leading-snug text-ink-3">{rest.join(" · ")}</p>
        )}
        <div className="mt-2 flex items-center gap-2 sm:hidden">
          <StampButton
            category={category}
            parent={parent}
            tilt={tilt}
            animate={animateStamp}
            onClick={onStamp}
          />
        </div>
        {account && <p className="eyebrow mt-1.5 !tracking-[0.12em]">{account}</p>}
      </div>

      <div className="hidden pt-0.5 sm:block">
        <StampButton
          category={category}
          parent={parent}
          tilt={tilt}
          animate={animateStamp}
          onClick={onStamp}
        />
      </div>

      <p
        className={`figures pt-0.5 text-right text-[1.05rem] ${credit ? "text-credit" : "text-debit"}`}
        aria-label={`${credit ? "In" : "Out"} ${formatSen(t.amount_sen)} ${t.currency}`}
      >
        <span className="mr-0.5 opacity-70">{signOf(t.amount_sen)}</span>
        {formatSen(t.amount_sen)}
        {t.currency !== "MYR" && <span className="ml-1 text-xs text-ink-3">{t.currency}</span>}
      </p>
    </article>
  );
}

function StampButton({
  category,
  parent,
  tilt,
  animate,
  onClick,
}: {
  category?: Category;
  parent?: Category;
  tilt: number;
  animate: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      data-stamp
      onClick={onClick}
      title={category ? `${parent ? `${parent.name} › ` : ""}${category.name}` : "Stamp a category"}
      className="block"
    >
      <AnimatePresence mode="popLayout" initial={false}>
        <motion.span
          key={category?.id ?? "empty"}
          className="stamp"
          data-empty={!category}
          data-income={category?.is_income ?? false}
          style={{ ["--tilt" as string]: `${tilt}deg` }}
          // A stamp lands: it comes down large and slightly turned, then thuds into place.
          initial={animate ? { scale: 1.9, opacity: 0, rotate: tilt - 9 } : false}
          animate={{ scale: 1, opacity: 0.92, rotate: tilt }}
          exit={{ opacity: 0, scale: 0.9, transition: { duration: 0.12 } }}
          transition={{ type: "spring", stiffness: 700, damping: 22, mass: 0.6 }}
        >
          {category ? category.name : "stamp"}
        </motion.span>
      </AnimatePresence>
    </button>
  );
}

function FilterSlip({
  value,
  onChange,
  accounts,
  categories,
  searchRef,
  filtered,
}: {
  value: Filters;
  onChange: (f: Filters) => void;
  accounts: Account[];
  categories: Category[];
  searchRef: React.RefObject<HTMLInputElement | null>;
  filtered: boolean;
}) {
  const set = (patch: Partial<Filters>) => onChange({ ...value, ...patch });
  const parents = categories.filter((c) => c.parent_id === null);

  return (
    <div className="perforated mt-8 grid grid-cols-2 gap-x-5 gap-y-3 rounded-b-md px-4 pb-4 pt-5 sm:grid-cols-[1.6fr_1fr_1fr_auto_auto] sm:items-end">
      <label className="col-span-2 sm:col-span-1">
        <span className="eyebrow">Search</span>
        <input
          ref={searchRef}
          value={value.q}
          onChange={(e) => set({ q: e.target.value })}
          placeholder="merchant, transfer note…"
          className="field mt-1 w-full"
          type="search"
        />
      </label>
      <label>
        <span className="eyebrow">Account</span>
        <select
          value={value.accountId}
          onChange={(e) => set({ accountId: e.target.value })}
          className="field mt-1 w-full"
        >
          <option value="">All accounts</option>
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span className="eyebrow">Category</span>
        <select
          value={value.category}
          onChange={(e) => set({ category: e.target.value })}
          className="field mt-1 w-full"
        >
          <option value="">All</option>
          <option value="none">Not stamped yet</option>
          {parents.map((p) => (
            <optgroup key={p.id} label={p.name}>
              <option value={p.id}>All {p.name}</option>
              {categories
                .filter((c) => c.parent_id === p.id)
                .map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
            </optgroup>
          ))}
        </select>
      </label>
      <div className="col-span-2 flex items-end gap-2 sm:col-span-1">
        <label className="flex-1">
          <span className="eyebrow">From</span>
          <input
            type="date"
            value={value.dateFrom}
            onChange={(e) => set({ dateFrom: e.target.value })}
            className="field figures mt-1 w-full text-sm"
          />
        </label>
        <label className="flex-1">
          <span className="eyebrow">To</span>
          <input
            type="date"
            value={value.dateTo}
            onChange={(e) => set({ dateTo: e.target.value })}
            className="field figures mt-1 w-full text-sm"
          />
        </label>
      </div>
      <button
        type="button"
        onClick={() => onChange(NO_FILTERS)}
        disabled={!filtered}
        className="col-span-2 justify-self-start pb-1.5 text-xs text-ink-3 underline-offset-4 enabled:hover:text-ink enabled:hover:underline disabled:opacity-0 sm:col-span-1"
      >
        clear
      </button>
    </div>
  );
}

function EmptyPage({ filtered, onClear }: { filtered: boolean; onClear: () => void }) {
  return (
    <div className="py-20 pl-24 pr-4">
      <p className="font-display text-3xl">
        {filtered ? "Nothing matches." : "A fresh page."}
      </p>
      <p className="mt-2 max-w-md text-ink-2">
        {filtered ? (
          <>
            No entries fit these filters.{" "}
            <button type="button" onClick={onClear} className="text-stamp underline underline-offset-4">
              Clear them
            </button>
            .
          </>
        ) : (
          "No entries printed yet. Import a bank statement and it will appear here, line by line."
        )}
      </p>
    </div>
  );
}
