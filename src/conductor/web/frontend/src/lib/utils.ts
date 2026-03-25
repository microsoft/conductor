export function cn(...classes: (string | undefined | false)[]): string {
  return classes.filter(Boolean).join(' ');
}

export function formatElapsed(seconds: number | undefined): string {
  if (seconds == null) return '';
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = (seconds % 60).toFixed(0);
  return `${m}m ${s}s`;
}

export function formatTokens(tokens: number | undefined): string {
  if (tokens == null) return '';
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}K`;
  return `${tokens}`;
}

export function formatCost(cost: number | undefined): string {
  if (cost == null) return '';
  return `$${cost.toFixed(4)}`;
}

export function formatOutput(output: unknown): string {
  if (output == null) return '';
  if (typeof output === 'string') return output;
  return JSON.stringify(output, null, 2);
}

export function formatContextFull(used: number, max: number): string {
  if (max <= 0) return `${used.toLocaleString()} tokens (limit unknown)`;
  const fmt = (n: number) => n.toLocaleString();
  const pct = ((used / max) * 100).toFixed(1);
  return `${fmt(used)} / ${fmt(max)} (${pct}%)`;
}
