import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

import { Sidebar } from "@/features/shell/Sidebar";
import { Providers } from "./providers";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter" });

export const metadata: Metadata = {
  title: "AI Agronomist",
  description: "Greenhouse monitoring and anomaly detection",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`${inter.variable} h-full antialiased`}>
      <body className="min-h-full">
        <Providers>
          <div className="flex">
            <Sidebar />
            <main className="min-h-dvh flex-1 overflow-x-hidden p-6">{children}</main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
