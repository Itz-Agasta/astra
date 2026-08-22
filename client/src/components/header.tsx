"use client";

import Link from "next/link";

import { ModeToggle } from "./mode-toggle";

export default function Header() {
  return (
    <header className="flex items-center justify-between border-b border-border px-3 py-1.5">
      <div className="flex items-center gap-3">
        <Link href="/" className="font-mono text-sm tracking-[0.22em]">
          ASTRA
        </Link>
        <span className="hidden text-xs text-muted-foreground sm:inline">Stellarium live view</span>
      </div>
      <ModeToggle />
    </header>
  );
}
