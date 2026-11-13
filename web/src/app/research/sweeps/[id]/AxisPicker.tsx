"use client";

import { useState } from "react";
import { Field } from "@/components/ui";

/** Which two settings the grid compares. A GET form, so it works without scripting and the choice lives in the URL; with
 * scripting, the setting chosen for one side is not offered on the other, so the same one can't be picked twice. */
export default function AxisPicker({ axes, x, y }: { axes: { path: string; label: string }[]; x: string; y: string }) {
  const [xv, setX] = useState(x);
  const [yv, setY] = useState(y);
  const options = (other: string) =>
    axes.map((a) => (
      <option key={a.path} value={a.path} disabled={a.path === other}>
        {a.label}
      </option>
    ));
  return (
    <form method="get" className="stk-sweep-pick" aria-label="Choose which two settings the grid compares">
      <Field label="Columns: setting across the top">
        <select className="stk-field" name="x" value={xv} onChange={(e) => setX(e.target.value)} data-fit>
          {options(yv)}
        </select>
      </Field>
      <Field label="Rows: setting down the side">
        <select className="stk-field" name="y" value={yv} onChange={(e) => setY(e.target.value)} data-fit>
          {options(xv)}
        </select>
      </Field>
      <button type="submit" className="stk-btn" data-tone="primary">
        Update grid
      </button>
    </form>
  );
}
