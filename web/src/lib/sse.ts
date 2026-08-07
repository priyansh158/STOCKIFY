/**
 * Lightweight SSE consumer for the analysis pipeline.
 *
 * Wraps `fetch()` + a manual ReadableStream parser instead of the browser
 * `EventSource` because:
 *   1. We need the request to share Next.js's proxy rewrites (relative URL).
 *   2. EventSource doesn't expose abort cleanly and can't send POST.
 *
 * Each `data: <json>\n\n` frame is JSON.parsed and handed to `onEvent`. The
 * stream ends when:
 *   - the server closes the connection (terminal `done` or `error` event), OR
 *   - the caller calls the returned `cancel()` function.
 */

import type { SSEEvent } from "@/lib/types";

export interface OpenStreamOpts {
  url: string;
  onEvent: (event: SSEEvent) => void;
  onClose?: (reason: "complete" | "aborted" | "error", err?: unknown) => void;
}

export function openAnalysisStream({ url, onEvent, onClose }: OpenStreamOpts) {
  const ctrl = new AbortController();

  (async () => {
    try {
      const res = await fetch(url, {
        signal: ctrl.signal,
        headers: { Accept: "text/event-stream" },
      });
      if (!res.ok || !res.body) {
        onClose?.("error", new Error(`SSE open failed: ${res.status}`));
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) {
          onClose?.("complete");
          return;
        }
        buffer += decoder.decode(value, { stream: true });

        // SSE frames are separated by blank lines.
        let idx: number;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);

          // Each frame has zero or more "field: value" lines. We only care
          // about `data:`. There can be multiple data lines per frame; the
          // SSE spec says they're joined with newlines.
          const dataParts: string[] = [];
          for (const line of frame.split("\n")) {
            if (line.startsWith("data:")) {
              dataParts.push(line.slice(5).trimStart());
            }
          }
          if (dataParts.length === 0) continue;
          const joined = dataParts.join("\n");
          try {
            onEvent(JSON.parse(joined) as SSEEvent);
          } catch {
            // Drop malformed event silently — server contract says JSON.
          }
        }
      }
    } catch (e) {
      if ((e as Error).name === "AbortError") {
        onClose?.("aborted");
      } else {
        onClose?.("error", e);
      }
    }
  })();

  return () => ctrl.abort();
}
