import { ref, watch } from "vue";

export function usePersistedSearch(key: string) {
  const search = ref("");
  try {
    search.value = window.localStorage.getItem(key) ?? "";
  } catch {
    // Search still works when browser storage is unavailable.
  }

  watch(search, (value) => {
    try {
      if (value) {
        window.localStorage.setItem(key, value);
      } else {
        window.localStorage.removeItem(key);
      }
    } catch {
      // Storage restrictions must not prevent filtering or clearing.
    }
  }, { flush: "sync" });

  return search;
}
