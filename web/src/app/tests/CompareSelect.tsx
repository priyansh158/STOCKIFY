"use client";

/**
 * The picker on the runs list: the table goes inside as `children`, with one checkbox named `ids` per finished run.
 * The form is a plain GET to /tests/compare, so the browser itself sends the repeated `ids`. This component only counts the
 * ticked boxes, keeps the choice to two to six (a seventh box is disabled until one is cleared) and enables the button.
 * Without scripts the form still works; the page then says plainly what to fix when the count is wrong.
 */
import { useId, useState, type ChangeEvent, type ReactNode } from "react";
import { COMPARE_MAX, COMPARE_MIN } from "@/lib/researchHelpers";

export default function CompareSelect({ children }: { children: ReactNode }) {
  const [count, setCount] = useState(0);
  const hint = useId();

  function onChange(e: ChangeEvent<HTMLFormElement>) {
    const boxes = Array.from(e.currentTarget.querySelectorAll<HTMLInputElement>('input[name="ids"]'));
    const ticked = boxes.filter((b) => b.checked).length;
    for (const b of boxes) b.disabled = !b.checked && ticked >= COMPARE_MAX;
    setCount(ticked);
  }

  const ok = count >= COMPARE_MIN && count <= COMPARE_MAX;
  const need =
    count < COMPARE_MIN
      ? `Tick ${COMPARE_MIN - count} more finished test${COMPARE_MIN - count === 1 ? "" : "s"} to compare.`
      : count === COMPARE_MAX
        ? `That is the most you can compare (${COMPARE_MAX}).`
        : "Ready to compare.";

  return (
    <form action="/tests/compare" method="get" onChange={onChange}>
      <div className="stk-pick-bar">
        <button type="submit" className="stk-btn" data-tone="primary" disabled={!ok} aria-describedby={hint}>
          Compare ({COMPARE_MIN}–{COMPARE_MAX})
        </button>
        <p id={hint} className="stk-label" role="status">
          {count} selected. {need}
        </p>
      </div>
      {children}
    </form>
  );
}
