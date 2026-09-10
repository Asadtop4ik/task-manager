import type { ReactNode } from "react";

/**
 * The reading column.
 *
 * Left-aligned against the nav rail, not centred in what is left over: centring
 * it on a wide monitor leaves a gap between the rail and the content and the
 * page reads as though it slid loose. `wide` is for the board, where four
 * columns genuinely need the room; everything else is a list of sentences and
 * stops at a measure you can read across.
 */
export default function Page({
  children,
  wide = false,
}: {
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <div className={["w-full", wide ? "max-w-6xl" : "max-w-4xl"].join(" ")}>
      {children}
    </div>
  );
}
