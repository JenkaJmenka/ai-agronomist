"use client";

import { QueryClientProvider } from "@tanstack/react-query";
import { NuqsAdapter } from "nuqs/adapters/next/app";
import { type ReactNode, useEffect, useState } from "react";

import { isProfileEnabled } from "@/lib/profiling";
import { createQueryClient } from "@/lib/query";

export function Providers({ children }: { readonly children: ReactNode }) {
  const [queryClient] = useState(createQueryClient);
  // Warm PROFILE_LOGGING_ENABLED cache off the interaction path.
  useEffect(() => {
    void isProfileEnabled();
  }, []);
  return (
    <NuqsAdapter>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </NuqsAdapter>
  );
}
