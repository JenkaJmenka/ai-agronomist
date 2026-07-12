"use client";

import { QueryClientProvider } from "@tanstack/react-query";
import { NuqsAdapter } from "nuqs/adapters/next/app";
import { type ReactNode, useState } from "react";

import { createQueryClient } from "@/lib/query";

export function Providers({ children }: { readonly children: ReactNode }) {
  const [queryClient] = useState(createQueryClient);
  return (
    <NuqsAdapter>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </NuqsAdapter>
  );
}
