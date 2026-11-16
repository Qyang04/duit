"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import type { Category } from "@/api/client";

export type PickerTarget = { txnId: string; current: string | null; rect: DOMRect };

/** The stamp drawer: pick a category for a transaction. A native popover, so the browser gives
 *  us the top layer, Escape and click-outside-to-close for free. Type to filter, Enter stamps the
 *  first match. */
export function StampPicker({
  target,
  categories,
  onPick,
  onClose,
}: {
  target: PickerTarget | null;
  categories: Category[];
  onPick: (txnId: string, categoryId: string | null) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (target) {
      setQuery("");
      const width = Math.min(360, window.innerWidth - 24);
      const left = Math.max(12, Math.min(target.rect.right - width, window.innerWidth - width - 12));
      const below = target.rect.bottom + 8;
      const top = below + 380 > window.innerHeight ? Math.max(12, target.rect.top - 388) : below;
      Object.assign(el.style, { top: `${top}px`, left: `${left}px`, width: `${width}px` });
      if (!el.matches(":popover-open")) el.showPopover();
      search.current?.focus();
    } else if (el.matches(":popover-open")) {
      el.hidePopover();
    }
  }, [target]);

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const parents = categories.filter((c) => c.parent_id === null);
    return parents
      .map((parent) => {
        const children = categories.filter((c) => c.parent_id === parent.id);
        const parentHit = parent.name.toLowerCase().includes(q);
        const shown = q && !parentHit ? children.filter((c) => c.name.toLowerCase().includes(q)) : children;
        return { parent, children: shown, visible: !q || parentHit || shown.length > 0 };
      })
      .filter((g) => g.visible)
      .sort((a, b) => Number(b.parent.is_income) - Number(a.parent.is_income) || a.parent.name.localeCompare(b.parent.name));
  }, [categories, query]);

  const first = groups[0] ? (groups[0].children[0] ?? groups[0].parent) : undefined;

  function pick(categoryId: string | null) {
    if (target) onPick(target.txnId, categoryId);
  }

  return (
    <div
      ref={ref}
      popover="auto"
      onToggle={(e) => {
        if ((e as unknown as { newState: string }).newState === "closed") onClose();
      }}
      style={{ position: "fixed", inset: "auto" }}
    >
      <div className="max-h-[23rem] overflow-hidden rounded-lg border border-paper-3 bg-paper shadow-[0_24px_48px_-16px_rgb(var(--shadow)/0.45)]">
        <div className="flex items-center gap-2 border-b border-paper-3 px-3 py-2.5">
          <span className="eyebrow">Stamp</span>
          <input
            ref={search}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && first) {
                e.preventDefault();
                pick(first.id);
              }
            }}
            placeholder="Groceries, fuel, salary…"
            className="field w-full text-sm"
            aria-label="Filter categories"
          />
        </div>
        <div className="max-h-[18.5rem] space-y-3 overflow-y-auto px-3 py-3">
          {groups.map(({ parent, children }) => (
            <section key={parent.id}>
              <button
                type="button"
                onClick={() => pick(parent.id)}
                className="eyebrow mb-1.5 block hover:text-ink"
              >
                {parent.name}
              </button>
              <div className="flex flex-wrap gap-1.5">
                {children.map((c) => (
                  <button
                    key={c.id}
                    type="button"
                    onClick={() => pick(c.id)}
                    className={`rounded-full border px-2.5 py-1 text-xs transition hover:-translate-y-px ${
                      target?.current === c.id
                        ? "border-stamp bg-stamp text-paper"
                        : "border-paper-3 text-ink-2 hover:border-ink-3 hover:text-ink"
                    } ${c.id === first?.id && query ? "ring-2 ring-stamp/40" : ""}`}
                  >
                    {c.name}
                  </button>
                ))}
              </div>
            </section>
          ))}
          {groups.length === 0 && (
            <p className="py-6 text-center text-sm text-ink-3">No category called “{query}”.</p>
          )}
        </div>
        {target?.current && (
          <button
            type="button"
            onClick={() => pick(null)}
            className="w-full border-t border-paper-3 px-3 py-2 text-left text-xs text-chop hover:bg-paper-2"
          >
            Remove stamp
          </button>
        )}
      </div>
    </div>
  );
}
