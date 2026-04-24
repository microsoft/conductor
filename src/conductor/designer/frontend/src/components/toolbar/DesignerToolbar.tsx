/**
 * Designer toolbar — add nodes, save, export, undo/redo.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useDesignerStore } from '@/stores/designer-store';
import { useExportYaml, useImportYaml, useSave } from '@/hooks/useDesignerApi';
import type { AgentDef } from '@/types/designer';

const NODE_TYPES = [
  { label: 'Agent', type: 'agent' as const, color: 'bg-blue-600' },
  { label: 'Gate', type: 'human_gate' as const, color: 'bg-amber-600' },
  { label: 'Script', type: 'script' as const, color: 'bg-green-600' },
  { label: 'Workflow', type: 'workflow' as const, color: 'bg-purple-600' },
] as const;

export function DesignerToolbar() {
  const config = useDesignerStore((s) => s.config);
  const filePath = useDesignerStore((s) => s.filePath);
  const dirty = useDesignerStore((s) => s.dirty);
  const addAgent = useDesignerStore((s) => s.addAgent);
  const undo = useDesignerStore((s) => s.undo);
  const redo = useDesignerStore((s) => s.redo);
  const canUndo = useDesignerStore((s) => s.canUndo);
  const canRedo = useDesignerStore((s) => s.canRedo);
  const toggleYaml = useDesignerStore((s) => s.toggleYamlPreview);
  const showYaml = useDesignerStore((s) => s.showYamlPreview);

  const exportYaml = useExportYaml();
  const importYaml = useImportYaml();
  const save = useSave();

  const [showAddMenu, setShowAddMenu] = useState(false);
  const [saving, setSaving] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Generate unique agent name
  const generateName = useCallback(
    (prefix: string) => {
      const existing = new Set(config.agents.map((a) => a.name));
      let i = 1;
      while (existing.has(`${prefix}_${i}`)) i++;
      return `${prefix}_${i}`;
    },
    [config.agents],
  );

  const handleAddNode = useCallback(
    (type: 'agent' | 'human_gate' | 'script' | 'workflow') => {
      const name = generateName(type === 'human_gate' ? 'gate' : type);
      const agent: AgentDef = { name };

      switch (type) {
        case 'agent':
          agent.prompt = 'Describe what this agent should do.';
          break;
        case 'human_gate':
          agent.type = 'human_gate';
          agent.prompt = 'Choose an option:';
          agent.options = [
            { label: 'Continue', route: '$end', description: 'Proceed with the workflow' },
          ];
          break;
        case 'script':
          agent.type = 'script';
          agent.command = 'echo "hello"';
          break;
        case 'workflow':
          agent.type = 'workflow';
          agent.workflow = './sub-workflow.yaml';
          break;
      }

      addAgent(agent);
      setShowAddMenu(false);
    },
    [addAgent, generateName],
  );

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      await save(config, filePath);
    } catch (err) {
      console.error('Save failed:', err);
    } finally {
      setSaving(false);
    }
  }, [config, filePath, save]);

  const handleExport = useCallback(async () => {
    try {
      const yaml = await exportYaml(config);
      const blob = new Blob([yaml], { type: 'text/yaml' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${config.workflow.name || 'workflow'}.yaml`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error('Export failed:', err);
    }
  }, [config, exportYaml]);

  // Ctrl+S keyboard shortcut
  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault();
        handleSave();
      }
      if ((e.ctrlKey || e.metaKey) && e.key === 'z') {
        e.preventDefault();
        if (e.shiftKey) {
          redo();
        } else {
          undo();
        }
      }
    },
    [handleSave, undo, redo],
  );

  // Register keyboard shortcuts
  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [handleKeyDown]);

  // Handle YAML file import
  const handleImportFile = useCallback(
    async (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (!file) return;
      const text = await file.text();
      try {
        await importYaml(text);
      } catch (err) {
        console.error('Import failed:', err);
      }
      // Reset input so same file can be re-imported
      e.target.value = '';
    },
    [importYaml],
  );

  return (
    <div className="flex items-center gap-2 px-4 py-2 bg-gray-900 border-b border-gray-700">
      {/* Logo / title */}
      <div className="flex items-center gap-2 mr-4">
        <span className="text-lg">🎨</span>
        <span className="font-bold text-gray-100">Designer</span>
        {dirty && <span className="text-xs text-amber-400">● unsaved</span>}
      </div>

      {/* Add node */}
      <div className="relative">
        <button
          onClick={() => setShowAddMenu(!showAddMenu)}
          className="px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white text-sm rounded font-medium"
        >
          + Add Node
        </button>
        {showAddMenu && (
          <div className="absolute top-full left-0 mt-1 bg-gray-800 border border-gray-600 rounded shadow-xl z-50 min-w-[150px]">
            {NODE_TYPES.map((nt) => (
              <button
                key={nt.type}
                onClick={() => handleAddNode(nt.type)}
                className="w-full text-left px-3 py-2 text-sm hover:bg-gray-700 flex items-center gap-2"
              >
                <span className={`w-2 h-2 rounded-full ${nt.color}`} />
                {nt.label}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="w-px h-6 bg-gray-700" />

      {/* Undo / Redo */}
      <button
        onClick={undo}
        disabled={!canUndo()}
        className="px-2 py-1.5 text-sm text-gray-300 hover:text-white disabled:text-gray-600 disabled:cursor-not-allowed"
        title="Undo (Ctrl+Z)"
      >
        ↩
      </button>
      <button
        onClick={redo}
        disabled={!canRedo()}
        className="px-2 py-1.5 text-sm text-gray-300 hover:text-white disabled:text-gray-600 disabled:cursor-not-allowed"
        title="Redo (Ctrl+Shift+Z)"
      >
        ↪
      </button>

      <div className="w-px h-6 bg-gray-700" />

      {/* YAML preview toggle */}
      <button
        onClick={toggleYaml}
        className={`px-3 py-1.5 text-sm rounded ${
          showYaml
            ? 'bg-gray-700 text-white'
            : 'text-gray-400 hover:text-white'
        }`}
      >
        YAML
      </button>

      <div className="flex-1" />

      {/* Import / Save / Export */}
      <input
        ref={fileInputRef}
        type="file"
        accept=".yaml,.yml"
        onChange={handleImportFile}
        className="hidden"
      />
      <button
        onClick={() => fileInputRef.current?.click()}
        className="px-3 py-1.5 text-sm text-gray-300 hover:text-white"
      >
        Import ↑
      </button>
      <button
        onClick={handleExport}
        className="px-3 py-1.5 text-sm text-gray-300 hover:text-white"
      >
        Export ↓
      </button>
      <button
        onClick={handleSave}
        disabled={saving}
        className="px-3 py-1.5 bg-green-600 hover:bg-green-700 text-white text-sm rounded font-medium disabled:opacity-50"
      >
        {saving ? 'Saving…' : 'Save'}
      </button>
    </div>
  );
}
