"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { api, ApiError } from "@/api/client";
import { useMe } from "@/lib/queries";

type Theme = "light" | "dark";

function readTheme(): Theme {
  try {
    const saved = localStorage.getItem("duit:theme");
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    /* storage blocked: fall back to the system setting */
  }
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>("light");
  useEffect(() => {
    const t = readTheme();
    setTheme(t);
    document.documentElement.dataset.theme = t;
  }, []);
  function toggle() {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("duit:theme", next);
    } catch {
      /* fine: the choice just will not be remembered */
    }
  }
  return [theme, toggle];
}

const icon = "h-[22px] w-[22px] fill-none stroke-current [stroke-linecap:round] [stroke-linejoin:round]";

function LedgerIcon() {
  return (
    <svg viewBox="0 0 24 24" className={icon} strokeWidth={1.6} aria-hidden>
      <path d="M5 4.5h10.5a3 3 0 0 1 3 3V20H8a3 3 0 0 1-3-3z" />
      <path d="M5 17a3 3 0 0 1 3-3h10.5M9 8h6M9 11h4" />
    </svg>
  );
}

function SunIcon() {
  return (
    <svg viewBox="0 0 24 24" className={icon} strokeWidth={1.6} aria-hidden>
      <circle cx="12" cy="12" r="3.6" />
      <path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" className={icon} strokeWidth={1.6} aria-hidden>
      <path d="M19 14.5A7.5 7.5 0 0 1 9.5 5a7.5 7.5 0 1 0 9.5 9.5z" />
    </svg>
  );
}

function DoorIcon() {
  return (
    <svg viewBox="0 0 24 24" className={icon} strokeWidth={1.6} aria-hidden>
      <path d="M14 4H6.5v16H14M10.5 12H20M17 8.5l3.5 3.5-3.5 3.5" />
    </svg>
  );
}

function SpineButton({
  label,
  active,
  onClick,
  children,
}: {
  label: string;
  active?: boolean;
  onClick?: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      aria-current={active ? "page" : undefined}
      className={`group relative grid h-11 w-11 place-items-center rounded-lg transition ${
        active ? "bg-[#d9b76c]/15 text-[#e7cb8a]" : "text-[#efe6d2]/55 hover:text-[#efe6d2]"
      }`}
    >
      {children}
      {/* The label slides out like a bookmark ribbon. */}
      <span className="pointer-events-none absolute left-full z-20 ml-3 -translate-x-1 whitespace-nowrap rounded-sm bg-ink px-2 py-1 text-xs text-paper opacity-0 transition group-hover:translate-x-0 group-hover:opacity-100 group-focus-visible:translate-x-0 group-focus-visible:opacity-100 max-md:hidden">
        {label}
      </span>
    </button>
  );
}

/** Logged-in pages: the spine on the left (bottom bar on phones), and an auth guard. */
export function Shell({ children }: { children: ReactNode }) {
  const me = useMe();
  const router = useRouter();
  const qc = useQueryClient();
  const [theme, toggleTheme] = useTheme();

  useEffect(() => {
    if (me.error instanceof ApiError && me.error.status === 401) router.replace("/login/");
  }, [me.error, router]);

  async function logout() {
    await api.POST("/api/auth/logout");
    qc.clear();
    router.replace("/login/");
  }

  if (!me.data) {
    return (
      <div className="grid min-h-dvh place-items-center">
        <p className="figures text-sm text-ink-3">
          {me.error && !(me.error instanceof ApiError && me.error.status === 401)
            ? "Cannot reach Duit right now."
            : "opening ledger"}{" "}
          <span className="cursor" />
        </p>
      </div>
    );
  }

  return (
    <div className="md:pl-[4.75rem]">
      <nav
        aria-label="Main"
        className="cover fixed inset-x-0 bottom-0 z-30 flex items-center justify-around px-3 py-2 md:inset-y-0 md:left-0 md:right-auto md:w-[4.75rem] md:flex-col md:justify-start md:gap-2 md:px-0 md:py-5"
      >
        <div className="hidden pb-4 md:block">
          <span className="foil font-display text-3xl">d</span>
        </div>
        <SpineButton label="Ledger" active>
          <LedgerIcon />
        </SpineButton>
        <div className="md:mt-auto" />
        <SpineButton label={theme === "dark" ? "Paper (light)" : "Night ledger (dark)"} onClick={toggleTheme}>
          {theme === "dark" ? <SunIcon /> : <MoonIcon />}
        </SpineButton>
        <SpineButton label="Close the book (log out)" onClick={() => void logout()}>
          <DoorIcon />
        </SpineButton>
      </nav>
      <div className="pb-20 md:pb-0">{children}</div>
    </div>
  );
}
