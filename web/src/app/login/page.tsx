"use client";

import { useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "motion/react";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { api, ApiError, unwrap, type User } from "@/api/client";
import { OtpInput } from "@/components/OtpInput";

type Step = "password" | "code" | "opening";

// Two different keyframe lists: alternating them makes motion replay the shake on every failure.
const SHAKES = [
  [0, -10, 9, -6, 4, 0],
  [0, 10, -9, 6, -4, 0],
];

/** A blind-embossed seal on the cover: "save · record · spend" around RM. */
function Seal() {
  return (
    <svg viewBox="0 0 120 120" className="mx-auto mt-6 h-28 w-28 opacity-40" aria-hidden>
      <defs>
        <path id="ring" d="M60 60m-43 0a43 43 0 1 1 86 0a43 43 0 1 1-86 0" />
      </defs>
      <circle cx="60" cy="60" r="56" fill="none" stroke="#d9b76c" strokeWidth="1" />
      <circle cx="60" cy="60" r="34" fill="none" stroke="#d9b76c" strokeWidth="0.75" />
      <text fill="#d9b76c" fontSize="8.5" letterSpacing="3.2" fontFamily="var(--ff-mono)">
        <textPath href="#ring">SIMPAN · CATAT · BELANJA · SIMPAN · CATAT · BELANJA ·</textPath>
      </text>
      <text x="60" y="68" textAnchor="middle" fill="#d9b76c" fontSize="22" fontFamily="var(--ff-display)">
        RM
      </text>
    </svg>
  );
}

export default function LoginPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [step, setStep] = useState<Step>("password");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [shake, setShake] = useState(0);

  async function login(totp?: string) {
    setBusy(true);
    setError(null);
    try {
      const user = unwrap<User>(
        await api.POST("/api/auth/login", {
          body: { email, password, totp_code: totp ?? null },
        }),
      );
      queryClient.setQueryData(["me"], user);
      setStep("opening"); // the cover swings open, then we go in
      window.setTimeout(() => router.replace("/"), 900);
    } catch (e) {
      const message = e instanceof ApiError ? e.message : "Cannot reach Duit. Try again.";
      if (message === "TOTP code required") {
        setStep("code");
      } else {
        setError(message);
        setCode("");
        setShake((n) => n + 1);
      }
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (step === "password") void login();
    else if (code.length === 6) void login(code);
  }

  return (
    <main className="grid min-h-dvh place-items-center overflow-hidden px-4 py-10">
      <div className="relative" style={{ perspective: "1800px" }}>
        {/* The first page, revealed when the cover opens. */}
        <div className="absolute inset-0 flex flex-col justify-center rounded-r-xl rounded-l-sm bg-paper-2 p-10 shadow-inner">
          <p className="eyebrow">Muka surat 1</p>
          <p className="mt-3 font-display text-4xl">Selamat kembali.</p>
          <p className="mt-2 text-ink-2">Opening your ledger…</p>
        </div>

        <motion.form
          onSubmit={onSubmit}
          className="cover stitched relative flex h-[34rem] w-[22rem] max-w-[calc(100vw-2rem)] origin-left flex-col rounded-r-xl rounded-l-sm px-9 pb-9 pt-12 text-[#efe6d2] sm:w-[24rem]"
          style={{ transformStyle: "preserve-3d", backfaceVisibility: "hidden" }}
          initial={{ rotateY: -18, opacity: 0, y: 20 }}
          animate={
            step === "opening"
              ? { rotateY: -168, opacity: 1, y: 0 }
              : { rotateY: 0, opacity: 1, y: 0, x: shake === 0 ? 0 : SHAKES[shake % 2] }
          }
          transition={
            step === "opening"
              ? { duration: 0.85, ease: [0.6, 0.02, 0.25, 1] }
              : { type: "spring", stiffness: 140, damping: 18, x: { duration: 0.4 } }
          }
        >
          <div className="text-center">
            <p className="eyebrow !text-[#d9b76c]/70">Buku akaun · Passbook</p>
            <h1 className="foil mt-5 font-display text-7xl leading-none">duit</h1>
            <div className="mx-auto mt-5 h-px w-24 bg-gradient-to-r from-transparent via-[#d9b76c]/60 to-transparent" />
            <Seal />
          </div>

          <div className="mt-auto">
            <AnimatePresence mode="wait" initial={false}>
              {step === "code" ? (
                <motion.div
                  key="code"
                  initial={{ opacity: 0, y: 12 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -12 }}
                >
                  <p className="mb-3 text-sm text-[#efe6d2]/70">
                    Code from your authenticator app
                  </p>
                  <OtpInput
                    value={code}
                    onChange={setCode}
                    onComplete={(c) => void login(c)}
                    disabled={busy}
                  />
                  <button
                    type="button"
                    onClick={() => {
                      setStep("password");
                      setCode("");
                      setError(null);
                    }}
                    className="mt-4 text-xs text-[#efe6d2]/55 underline-offset-4 hover:underline"
                  >
                    ← Use a different account
                  </button>
                </motion.div>
              ) : (
                <motion.div
                  key="password"
                  className="space-y-3"
                  initial={{ opacity: 0, y: 12 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -12 }}
                >
                  <input
                    className="engraved w-full px-3.5 py-2.5"
                    type="email"
                    autoComplete="username"
                    placeholder="Email"
                    required
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    autoFocus
                  />
                  <input
                    className="engraved w-full px-3.5 py-2.5"
                    type="password"
                    autoComplete="current-password"
                    placeholder="Password"
                    required
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                  <button
                    type="submit"
                    disabled={busy}
                    className="group mt-2 flex w-full items-center justify-between rounded-md bg-[#d9b76c] px-4 py-2.5 font-semibold text-[#1b2440] shadow-[inset_0_-2px_0_rgb(0_0_0/0.2)] transition hover:bg-[#e6c886] disabled:opacity-60"
                  >
                    <span>{busy ? "Checking…" : "Open"}</span>
                    <span className="transition-transform group-hover:translate-x-1">→</span>
                  </button>
                </motion.div>
              )}
            </AnimatePresence>

            <p
              role="alert"
              className="mt-4 min-h-5 text-center text-sm text-[#f3a08c]"
              aria-live="polite"
            >
              {error}
            </p>
          </div>
        </motion.form>
      </div>
    </main>
  );
}
