import { useCallback, useEffect, useRef, useState } from "react";

/**
 * The shared lifecycle for a write to Steward.
 *
 * A generation belongs to the mounted editor. Superseded writes and answers that arrive
 * after navigation are ignored, so no page can update local state after it has gone away.
 */
export function useStewardWrite(write, { onStale, identity } = {}) {
  const [saving, setSaving] = useState(false);
  const [refusal, setRefusal] = useState(null);
  const [receipt, setReceipt] = useState(null);
  const generation = useRef(0);
  const identityRef = useRef(identity);
  const writeRef = useRef(write);
  const onStaleRef = useRef(onStale);

  if (identityRef.current !== identity) {
    identityRef.current = identity;
    generation.current += 1;
  }

  writeRef.current = write;
  onStaleRef.current = onStale;

  useEffect(() => {
    setSaving(false);
  }, [identity]);

  useEffect(
    () => () => {
      generation.current += 1;
    },
    [],
  );

  const reset = useCallback(() => {
    generation.current += 1;
    setSaving(false);
    setRefusal(null);
    setReceipt(null);
  }, []);

  const clearRefusal = useCallback(() => setRefusal(null), []);
  const clearReceipt = useCallback(() => setReceipt(null), []);

  const save = useCallback(async (draft) => {
    const current = ++generation.current;
    setSaving(true);
    setRefusal(null);
    setReceipt(null);
    let answer;
    try {
      answer = await writeRef.current(draft);
    } catch (caught) {
      if (current !== generation.current) return null;
      setRefusal(caught);
      if (caught?.code === "stale_revision") onStaleRef.current?.(caught, draft);
      return null;
    } finally {
      if (current === generation.current) setSaving(false);
    }
    if (current !== generation.current) return null;
    setReceipt(answer);
    return answer;
  }, []);

  return { saving, refusal, receipt, save, reset, clearRefusal, clearReceipt };
}
