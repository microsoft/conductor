import { ChevronRight, Layers } from 'lucide-react';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { BreadcrumbEntry } from '@/stores/workflow-store';

export function BreadcrumbBar() {
  const getBreadcrumbs = useWorkflowStore((s) => s.getBreadcrumbs);
  const navigateToContext = useWorkflowStore((s) => s.navigateToContext);
  const viewContextPath = useWorkflowStore((s) => s.viewContextPath);
  const subworkflowContexts = useWorkflowStore((s) => s.subworkflowContexts);

  // Only show if there are subworkflows
  if (subworkflowContexts.length === 0 && viewContextPath.length === 0) return null;

  const crumbs: BreadcrumbEntry[] = getBreadcrumbs();

  return (
    <div className="flex items-center gap-1 px-4 py-1.5 bg-[var(--surface)] border-b border-[var(--border)] text-xs flex-shrink-0">
      <Layers className="w-3 h-3 text-[var(--text-muted)] mr-1" />
      {crumbs.map((crumb, idx) => {
        const isLast = idx === crumbs.length - 1;
        const isActive = JSON.stringify(crumb.path) === JSON.stringify(viewContextPath);
        return (
          <span key={idx} className="flex items-center gap-1">
            {idx > 0 && <ChevronRight className="w-3 h-3 text-[var(--text-muted)]" />}
            {isLast ? (
              <span className="font-semibold text-[var(--text)]">
                {crumb.label}
              </span>
            ) : (
              <button
                onClick={() => navigateToContext(crumb.path)}
                className={`hover:text-[var(--running)] transition-colors ${
                  isActive ? 'text-[var(--text)] font-medium' : 'text-[var(--text-muted)]'
                }`}
              >
                {crumb.label}
              </button>
            )}
          </span>
        );
      })}
    </div>
  );
}
