/**
 * The part of a run id that tells runs of the same strategy apart: "orb_reliance_minute-ada883a0319e-2" with the display
 * name "orb_reliance_minute" gives "ada883a0319e-2". When the id does not start with the name, the whole id is returned.
 */
export function runIdTail(runId: string, name: string | null | undefined): string {
  if (name && runId.startsWith(`${name}-`) && runId.length > name.length + 1) return runId.slice(name.length + 1);
  return runId;
}
