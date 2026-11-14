import Link from "next/link";
import { PageHead } from "@/components/ui";

/** The page for an address STOCKIFY has no page at (in the app's own system, not the framework's black default). */
export default function NotFound() {
  return (
    <div className="stk-page" data-narrow="">
      <PageHead title="No page here" lede="STOCKIFY has no page at this address. It may have moved, or the link has a typo." />
      <Link href="/" className="stk-btn">
        Go to the home page
      </Link>
    </div>
  );
}
