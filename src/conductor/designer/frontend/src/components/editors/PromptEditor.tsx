/**
 * Prompt editor with basic Jinja2 template highlighting.
 *
 * Highlights {{ variable }} and {% control %} blocks with distinct colors.
 * Uses a transparent textarea overlaid on a highlighted div.
 */

import { useRef, useState, useCallback } from 'react';

interface Props {
  value: string;
  onChange: (value: string) => void;
  rows?: number;
  placeholder?: string;
}

/** Simple Jinja2-aware prompt editor. */
export function PromptEditor({ value, onChange, rows = 6, placeholder }: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [focused, setFocused] = useState(false);

  const handleChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      onChange(e.target.value);
    },
    [onChange],
  );

  return (
    <div className={`relative mt-0.5 rounded border ${focused ? 'border-blue-500' : 'border-gray-600'} bg-gray-800`}>
      {/* Highlighted preview (behind the textarea) */}
      <div
        className="absolute inset-0 px-2 py-1.5 text-sm font-mono whitespace-pre-wrap break-words overflow-hidden pointer-events-none"
        aria-hidden
      >
        {highlightJinja2(value)}
      </div>

      {/* Transparent textarea (on top, captures input) */}
      <textarea
        ref={textareaRef}
        value={value}
        onChange={handleChange}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        placeholder={placeholder ?? 'Enter prompt template…'}
        rows={rows}
        className="relative w-full bg-transparent text-transparent caret-gray-200 px-2 py-1.5 text-sm font-mono resize-y focus:outline-none"
        spellCheck={false}
      />
    </div>
  );
}

/** Highlight Jinja2 expressions in a prompt string. */
function highlightJinja2(text: string): React.ReactNode[] {
  const parts: React.ReactNode[] = [];
  // Match {{ ... }} and {% ... %} and {# ... #}
  const pattern = /(\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\})/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    // Text before match
    if (match.index > lastIndex) {
      parts.push(
        <span key={`t-${lastIndex}`} className="text-gray-200">
          {text.slice(lastIndex, match.index)}
        </span>,
      );
    }

    const token = match[0]!;
    let className = 'text-blue-400 bg-blue-900/30 rounded px-0.5';
    if (token.startsWith('{%')) {
      className = 'text-purple-400 bg-purple-900/30 rounded px-0.5';
    } else if (token.startsWith('{#')) {
      className = 'text-gray-500 bg-gray-700/30 rounded px-0.5';
    }

    parts.push(
      <span key={`m-${match.index}`} className={className}>
        {token}
      </span>,
    );
    lastIndex = match.index + token.length;
  }

  // Remaining text
  if (lastIndex < text.length) {
    parts.push(
      <span key={`t-${lastIndex}`} className="text-gray-200">
        {text.slice(lastIndex)}
      </span>,
    );
  }

  return parts.length > 0 ? parts : [<span key="empty" className="text-gray-500">{''}</span>];
}
