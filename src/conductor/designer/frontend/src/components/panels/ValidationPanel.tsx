/**
 * Validation panel — shows live errors and warnings.
 */

import { useDesignerStore } from '@/stores/designer-store';

export function ValidationPanel() {
  const validation = useDesignerStore((s) => s.validation);
  const show = useDesignerStore((s) => s.showValidationPanel);
  const toggle = useDesignerStore((s) => s.toggleValidationPanel);

  const hasErrors = validation.errors.length > 0;
  const hasWarnings = validation.warnings.length > 0;
  const isEmpty = !hasErrors && !hasWarnings;

  return (
    <div className="bg-gray-900 border-t border-gray-700">
      {/* Header bar — always visible */}
      <button
        onClick={toggle}
        className="w-full flex items-center gap-2 px-4 py-1.5 text-xs hover:bg-gray-800"
      >
        <span className="font-bold text-gray-400 uppercase tracking-wide">Problems</span>
        {hasErrors && (
          <span className="bg-red-600 text-white px-1.5 py-0.5 rounded-full text-[10px] font-bold">
            {validation.errors.length}
          </span>
        )}
        {hasWarnings && (
          <span className="bg-amber-600 text-white px-1.5 py-0.5 rounded-full text-[10px] font-bold">
            {validation.warnings.length}
          </span>
        )}
        {isEmpty && (
          <span className="text-green-500 text-[10px]">✓ Valid</span>
        )}
        <span className="ml-auto text-gray-500">{show ? '▼' : '▲'}</span>
      </button>

      {/* Expanded content */}
      {show && (
        <div className="max-h-40 overflow-y-auto px-4 pb-2">
          {isEmpty && (
            <p className="text-xs text-gray-500 italic py-1">No problems found.</p>
          )}
          {validation.errors.map((err, i) => (
            <div key={`e-${i}`} className="flex items-start gap-2 py-0.5">
              <span className="text-red-500 text-xs mt-0.5">✕</span>
              <span className="text-xs text-red-300">{err}</span>
            </div>
          ))}
          {validation.warnings.map((warn, i) => (
            <div key={`w-${i}`} className="flex items-start gap-2 py-0.5">
              <span className="text-amber-500 text-xs mt-0.5">⚠</span>
              <span className="text-xs text-amber-300">{warn}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
