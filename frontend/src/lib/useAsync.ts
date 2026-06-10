import { useCallback, useEffect, useState } from "react";

import { ApiError } from "../api";

export interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
}

/**
 * Run an async loader on mount (and whenever `deps` change), tracking
 * loading/error/data. `reload()` re-runs it on demand.
 */
export function useAsync<T>(
  loader: () => Promise<T>,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  deps: any[] = [],
): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(loader, deps);

  const execute = useCallback(() => {
    let active = true;
    setLoading(true);
    setError(null);
    run()
      .then((d) => active && setData(d))
      .catch((e) => {
        if (!active) return;
        setError(e instanceof ApiError ? e.message : String(e));
      })
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [run]);

  useEffect(() => execute(), [execute]);

  return { data, loading, error, reload: execute };
}
