import type { Metadata } from "next";
import { connection } from "next/server";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "Custombuild · Konstruktionsarbetsyta",
  description: "Parametrisk konstruktion och produktionsberedning för måttanpassade möbler.",
};

export default async function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  // A fresh CSP nonce exists only after proxy.ts receives a request. Forcing
  // dynamic rendering lets Next attach that nonce to its framework tags.
  await connection();

  return (
    <html lang="sv">
      <body>{children}</body>
    </html>
  );
}
