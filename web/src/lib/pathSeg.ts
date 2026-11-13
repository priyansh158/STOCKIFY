/**
 * Path segments for API calls. A URL parser treats "." and ".." as dot segments even when they are written %2E or %2e, and
 * folds them away ("/api/tests/%2E%2E/x" is "/api/x"), so no encoding makes such an id safe: it is refused. An id that
 * merely contains a dot or a "%" is fine (encodeURIComponent turns "%" into %25, which no parser reads as a dot).
 */

/** True when the segment, once percent-decoded, is "." or "..". */
export function isDotSegment(s: string): boolean {
  let d = s;
  try {
    d = decodeURIComponent(s);
  } catch {
    // not valid percent-encoding: it is sent encoded, as written
  }
  return d === "." || d === "..";
}

/** One id as one path segment, encoded; throws for an id that would fold into another path. */
export function pathSegment(s: string): string {
  if (isDotSegment(s)) throw new RangeError("that id cannot be used in a request path");
  return encodeURIComponent(s);
}

/** An id that keeps its "/" (the route is a path), every segment encoded; null when any segment is a dot segment. */
export function pathSegments(id: string): string | null {
  const parts = id.split("/");
  return parts.some(isDotSegment) ? null : parts.map(encodeURIComponent).join("/");
}
