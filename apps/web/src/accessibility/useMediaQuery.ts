import { useEffect, useState } from "react";

function matchesMediaQuery(query: string): boolean {
  return typeof window !== "undefined" &&
    typeof window.matchMedia === "function"
    ? window.matchMedia(query).matches
    : false;
}

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => matchesMediaQuery(query));

  useEffect(() => {
    if (
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function"
    ) {
      return undefined;
    }
    const mediaQuery = window.matchMedia(query);
    const update = (): void => setMatches(mediaQuery.matches);
    update();
    mediaQuery.addEventListener("change", update);
    return () => mediaQuery.removeEventListener("change", update);
  }, [query]);

  return matches;
}
