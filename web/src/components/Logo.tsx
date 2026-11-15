import Image from "next/image";

/**
 * The STOCKIFY lockup (mark and wordmark). `Logo` is the home page's heading (the image's alt text is the heading's name);
 * `LogoMark` is the mark alone. The top bar carries the lockup as a link on every other page (AppNav).
 */
export function Logo() {
  return (
    <h1 className="stk-logo">
      <Image src="/brand/lockup.png" alt="STOCKIFY" width={1622} height={510} priority unoptimized />
    </h1>
  );
}

export function LogoMark() {
  const height = 64; // intrinsic size; the stylesheet sets the shown size
  return (
    <Image
      className="stk-logomark"
      src="/brand/mark.png"
      alt="STOCKIFY"
      width={Math.round((height * 414) / 510)}
      height={height}
      unoptimized
    />
  );
}
