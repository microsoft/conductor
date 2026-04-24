/**
 * YAML preview panel — shows the generated YAML from current state.
 */

import { useEffect, useState } from 'react';
import { useDesignerStore } from '@/stores/designer-store';
import { useExportYaml } from '@/hooks/useDesignerApi';

export function YamlPreview() {
  const config = useDesignerStore((s) => s.config);
  const exportYaml = useExportYaml();
  const [yaml, setYaml] = useState('');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const result = await exportYaml(config);
        if (!cancelled) {
          setYaml(result);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Export failed');
        }
      }
    }, 300);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [config, exportYaml]);

  return (
    <div className="w-96 bg-gray-900 border-l border-gray-700 flex flex-col">
      <div className="px-4 py-2 border-b border-gray-700">
        <h2 className="text-sm font-bold text-gray-300 uppercase tracking-wide">
          YAML Preview
        </h2>
      </div>
      <div className="flex-1 overflow-auto p-4">
        {error ? (
          <p className="text-xs text-red-400">{error}</p>
        ) : (
          <pre className="text-xs text-gray-300 font-mono whitespace-pre-wrap">{yaml}</pre>
        )}
      </div>
    </div>
  );
}
