/**
 * Root application component for the Conductor Designer.
 *
 * Layout:
 * ┌─────────────────────────────────┐
 * │ Toolbar                         │
 * ├──────────┬──────────┬───────────┤
 * │          │          │ Property  │
 * │  Canvas  │ YAML     │ Panel     │
 * │          │ Preview  │           │
 * ├──────────┴──────────┴───────────┤
 * │ Validation Panel                │
 * └─────────────────────────────────┘
 */

import { ReactFlowProvider } from '@xyflow/react';
import { useDesignerStore } from '@/stores/designer-store';
import { DesignerCanvas } from '@/components/canvas/DesignerCanvas';
import { DesignerToolbar } from '@/components/toolbar/DesignerToolbar';
import { PropertyPanel } from '@/components/panels/PropertyPanel';
import { ValidationPanel } from '@/components/panels/ValidationPanel';
import { YamlPreview } from '@/components/preview/YamlPreview';

export function App() {
  const showYaml = useDesignerStore((s) => s.showYamlPreview);

  return (
    <ReactFlowProvider>
      <div className="flex flex-col h-screen w-screen bg-gray-950 text-gray-100">
        {/* Toolbar */}
        <DesignerToolbar />

        {/* Main content */}
        <div className="flex flex-1 overflow-hidden">
          {/* Canvas (takes remaining space) */}
          <DesignerCanvas />

          {/* YAML Preview (optional, toggle) */}
          {showYaml && <YamlPreview />}

          {/* Property Panel (always visible on right) */}
          <PropertyPanel />
        </div>

        {/* Validation panel (bottom) */}
        <ValidationPanel />
      </div>
    </ReactFlowProvider>
  );
}
