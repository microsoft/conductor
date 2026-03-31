import { Play, Pause } from 'lucide-react';
import { useWorkflowStore } from '@/stores/workflow-store';
import { cn } from '@/lib/utils';

const SPEEDS = [1, 2, 4, 8] as const;

function formatRelativeTime(events: { timestamp: number }[], position: number): string {
  if (position === 0 || events.length === 0) return '+0.0s';
  const startTime = events[0]!.timestamp;
  const currentTime = events[Math.min(position, events.length) - 1]!.timestamp;
  const diff = currentTime - startTime;
  if (diff < 60) return `+${diff.toFixed(1)}s`;
  const mins = Math.floor(diff / 60);
  const secs = diff % 60;
  return `+${mins}m${secs.toFixed(0)}s`;
}

export function ReplayBar() {
  const replayPosition = useWorkflowStore((s) => s.replayPosition);
  const replayTotalEvents = useWorkflowStore((s) => s.replayTotalEvents);
  const replayPlaying = useWorkflowStore((s) => s.replayPlaying);
  const replaySpeed = useWorkflowStore((s) => s.replaySpeed);
  const replayEvents = useWorkflowStore((s) => s.replayEvents);
  const setReplayPosition = useWorkflowStore((s) => s.setReplayPosition);
  const setReplayPlaying = useWorkflowStore((s) => s.setReplayPlaying);
  const setReplaySpeed = useWorkflowStore((s) => s.setReplaySpeed);

  const handleSliderChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const pos = parseInt(e.target.value, 10);
    setReplayPosition(pos);
    // Pause when user manually scrubs
    if (replayPlaying) setReplayPlaying(false);
  };

  const togglePlayPause = () => {
    if (!replayPlaying && replayPosition >= replayTotalEvents) {
      // Restart from beginning if at end
      setReplayPosition(0);
    }
    setReplayPlaying(!replayPlaying);
  };

  const pct = replayTotalEvents > 0 ? (replayPosition / replayTotalEvents) * 100 : 0;

  return (
    <footer className="flex items-center gap-3 px-4 py-1.5 border-t bg-[var(--surface)] border-[var(--border)] text-xs flex-shrink-0">
      {/* Play/Pause button */}
      <button
        onClick={togglePlayPause}
        className="flex items-center justify-center w-6 h-6 rounded hover:bg-[var(--surface-hover)] text-[var(--text-secondary)] hover:text-[var(--text)] transition-colors"
        title={replayPlaying ? 'Pause' : 'Play'}
      >
        {replayPlaying ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
      </button>

      {/* Slider */}
      <div className="flex-1 relative flex items-center">
        <input
          type="range"
          min={0}
          max={replayTotalEvents}
          value={replayPosition}
          onChange={handleSliderChange}
          className="w-full h-1 appearance-none rounded-full cursor-pointer"
          style={{
            background: `linear-gradient(to right, var(--accent) 0%, var(--accent) ${pct}%, var(--border) ${pct}%, var(--border) 100%)`,
            WebkitAppearance: 'none',
          }}
        />
        <style>{`
          footer input[type="range"]::-webkit-slider-thumb {
            -webkit-appearance: none;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: var(--accent);
            border: 2px solid var(--surface);
            cursor: pointer;
            box-shadow: 0 0 4px rgba(99, 102, 241, 0.4);
          }
          footer input[type="range"]::-moz-range-thumb {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: var(--accent);
            border: 2px solid var(--surface);
            cursor: pointer;
            box-shadow: 0 0 4px rgba(99, 102, 241, 0.4);
          }
        `}</style>
      </div>

      {/* Timestamp */}
      <span className="text-[var(--text-muted)] font-mono whitespace-nowrap">
        {formatRelativeTime(replayEvents, replayPosition)}
      </span>

      {/* Event counter */}
      <span className="text-[var(--text-muted)] font-mono whitespace-nowrap">
        Event {replayPosition}/{replayTotalEvents}
      </span>

      {/* Speed buttons */}
      <div className="flex items-center gap-0.5">
        {SPEEDS.map((speed) => (
          <button
            key={speed}
            onClick={() => setReplaySpeed(speed)}
            className={cn(
              'px-1.5 py-0.5 rounded text-xs font-mono transition-colors',
              replaySpeed === speed
                ? 'bg-[var(--accent)] text-white'
                : 'text-[var(--text-muted)] hover:text-[var(--text-secondary)] hover:bg-[var(--surface-hover)]',
            )}
          >
            {speed}×
          </button>
        ))}
      </div>
    </footer>
  );
}
