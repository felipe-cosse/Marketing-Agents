import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { createBrowserRouter, RouterProvider } from "react-router-dom";

interface AppProvidersProps {
  readonly children: ReactNode;
}

export function AppProviders({
  children,
}: AppProvidersProps): React.JSX.Element {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            retry: false,
            refetchOnWindowFocus: false,
          },
          mutations: { retry: false },
        },
      }),
  );
  const [router] = useState(() =>
    createBrowserRouter([{ path: "*", element: children }]),
  );

  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}
