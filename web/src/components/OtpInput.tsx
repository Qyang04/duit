"use client";

import { useRef, type ClipboardEvent, type KeyboardEvent } from "react";

/** Six digit boxes, like the wheels of a combination lock. Typing advances, Backspace goes back,
 *  pasting "123456" (or "123 456") fills them all. The first box carries autocomplete so
 *  password managers and iOS can fill the whole code. */
export function OtpInput({
  value,
  onChange,
  onComplete,
  disabled,
}: {
  value: string;
  onChange: (code: string) => void;
  onComplete: (code: string) => void;
  disabled?: boolean;
}) {
  const boxes = useRef<(HTMLInputElement | null)[]>([]);
  const digits = Array.from({ length: 6 }, (_, i) => value[i] ?? "");

  function set(code: string, focus: number) {
    const clean = code.replace(/\D/g, "").slice(0, 6);
    onChange(clean);
    boxes.current[Math.min(focus, 5)]?.focus();
    if (clean.length === 6) onComplete(clean);
  }

  function onInput(i: number, raw: string) {
    const typed = raw.replace(/\D/g, "");
    if (!typed) return;
    // The code is the digits typed so far: typing into box i keeps what is before it.
    const next = (value.slice(0, i) + typed).slice(0, 6);
    set(next, next.length);
  }

  function onKeyDown(i: number, e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Backspace") {
      e.preventDefault();
      const next = value.slice(0, Math.max(0, Math.min(i + 1, value.length) - 1));
      onChange(next);
      boxes.current[next.length]?.focus();
    } else if (e.key === "ArrowLeft") boxes.current[i - 1]?.focus();
    else if (e.key === "ArrowRight") boxes.current[i + 1]?.focus();
  }

  function onPaste(e: ClipboardEvent<HTMLInputElement>) {
    e.preventDefault();
    const pasted = e.clipboardData.getData("text");
    set(pasted, pasted.replace(/\D/g, "").length);
  }

  return (
    <div className="flex justify-between gap-2" role="group" aria-label="Six-digit code">
      {digits.map((d, i) => (
        <input
          key={i}
          ref={(el) => {
            boxes.current[i] = el;
          }}
          value={d}
          disabled={disabled}
          inputMode="numeric"
          autoComplete={i === 0 ? "one-time-code" : "off"}
          maxLength={i === 0 ? 6 : 1}
          aria-label={`Digit ${i + 1}`}
          onChange={(e) => onInput(i, e.target.value)}
          onKeyDown={(e) => onKeyDown(i, e)}
          onPaste={onPaste}
          onFocus={(e) => e.target.select()}
          className={`engraved figures h-14 w-11 text-center text-2xl transition-transform ${
            d ? "-translate-y-0.5" : ""
          } ${i === 2 ? "mr-3" : ""}`}
        />
      ))}
    </div>
  );
}
