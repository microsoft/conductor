/**
 * React hooks for communicating with the designer backend API.
 */

import { useCallback, useRef } from 'react';
import type { WorkflowConfig, ValidationResult } from '@/types/designer';
import { useDesignerStore } from '@/stores/designer-store';

const API_BASE = '';

async function apiFetch<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error ?? `API error: ${res.status}`);
  }
  return res.json() as Promise<T>;
}

/** Load the workflow from the server on mount. */
export function useLoadWorkflow() {
  const setConfig = useDesignerStore((s) => s.setConfig);

  return useCallback(async () => {
    const data = await apiFetch<{ workflow: WorkflowConfig; path: string | null }>(
      '/api/workflow',
    );
    setConfig(data.workflow, data.path);
  }, [setConfig]);
}

/** Validate the current workflow config. */
export function useValidate() {
  const setValidation = useDesignerStore((s) => s.setValidation);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  return useCallback(
    (config: WorkflowConfig, debounceMs = 500) => {
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(async () => {
        try {
          const result = await apiFetch<ValidationResult>('/api/validate', {
            method: 'POST',
            body: JSON.stringify({ workflow: config }),
          });
          setValidation(result);
        } catch {
          // Silently ignore validation errors during typing
        }
      }, debounceMs);
    },
    [setValidation],
  );
}

/** Export workflow config to YAML string. */
export function useExportYaml() {
  return useCallback(async (config: WorkflowConfig): Promise<string> => {
    const data = await apiFetch<{ yaml: string }>('/api/export', {
      method: 'POST',
      body: JSON.stringify({ workflow: config }),
    });
    return data.yaml;
  }, []);
}

/** Import YAML text into a WorkflowConfig. */
export function useImportYaml() {
  const setConfig = useDesignerStore((s) => s.setConfig);

  return useCallback(
    async (yaml: string) => {
      const data = await apiFetch<{ workflow: WorkflowConfig }>('/api/import', {
        method: 'POST',
        body: JSON.stringify({ yaml }),
      });
      setConfig(data.workflow);
    },
    [setConfig],
  );
}

/** Save workflow to disk. */
export function useSave() {
  return useCallback(async (config: WorkflowConfig, path?: string | null) => {
    await apiFetch('/api/save', {
      method: 'POST',
      body: JSON.stringify({
        workflow: config,
        path: path ?? undefined,
      }),
    });
  }, []);
}
