import { useEffect, useRef, useCallback } from 'react';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { WorkflowEvent } from '@/types/events';
import { withToken } from '@/lib/auth';

const MAX_RECONNECT_DELAY = 30000;

export function useWebSocket() {
  const processEvent = useWorkflowStore((s) => s.processEvent);
  const replayState = useWorkflowStore((s) => s.replayState);
  const setWsStatus = useWorkflowStore((s) => s.setWsStatus);
  const setWsSend = useWorkflowStore((s) => s.setWsSend);
  const setWsAuthFailed = useWorkflowStore((s) => s.setWsAuthFailed);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectDelayRef = useRef(1000);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fetchAbortRef = useRef<AbortController | null>(null);
  // Use a ref to break the circular dependency between connect and scheduleReconnect
  const connectRef = useRef<() => void>(() => {});

  const scheduleReconnect = useCallback(() => {
    setWsStatus('reconnecting');
    reconnectTimerRef.current = setTimeout(() => {
      reconnectDelayRef.current = Math.min(
        reconnectDelayRef.current * 2,
        MAX_RECONNECT_DELAY,
      );
      connectRef.current();
    }, reconnectDelayRef.current);
  }, [setWsStatus]);

  const connect = useCallback(() => {
    setWsStatus('connecting');

    // Cancel any in-flight fetch from a previous connect attempt
    if (fetchAbortRef.current) {
      fetchAbortRef.current.abort();
    }
    const abortController = new AbortController();
    fetchAbortRef.current = abortController;

    // Always fetch full state before opening WebSocket (handles initial + reconnect)
    fetch('/api/state', { signal: abortController.signal })
      .then((resp) => {
        if (!resp.ok) {
          // `/api/state` is unauthenticated but still Origin/Host-guarded
          // (issue #397); a non-2xx here means the server is up but
          // rejected the request, not "state happens to be empty" — fetch
          // resolves (doesn't reject) on 4xx/5xx, so this must be checked
          // explicitly or a 403 body would silently parse as `undefined`
          // and the dashboard would render as though state were empty.
          throw new Error(`GET /api/state -> ${resp.status}`);
        }
        return resp.json() as Promise<WorkflowEvent[]>;
      })
      .then((events: WorkflowEvent[]) => {
        if (events && events.length > 0) {
          replayState(events);
        }

        const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = withToken(`${proto}//${window.location.host}/ws`);

        try {
          const ws = new WebSocket(wsUrl);
          wsRef.current = ws;
          // Whether this attempt's handshake ever completed. A WebSocket
          // rejected at the auth guard (issue #397) never fires `onopen`
          // and closes with the same code 1006 a dead process would
          // produce -- indistinguishable at the browser API level. Since
          // the preceding `/api/state` fetch just confirmed the server is
          // reachable, an immediate close with no `onopen` is a strong
          // signal the rejection is at the WebSocket handshake itself
          // (wrong/missing token, or a Host/Origin mismatch on `/ws`
          // specifically), not a crashed process.
          let didOpen = false;

          ws.onopen = () => {
            didOpen = true;
            reconnectDelayRef.current = 1000;
            setWsStatus('connected');
            setWsAuthFailed(false);
            // Expose send function to the store
            setWsSend((data: object) => {
              if (ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify(data));
              }
            });
          };

          ws.onmessage = (evt) => {
            try {
              const event = JSON.parse(evt.data) as WorkflowEvent;
              processEvent(event);
            } catch (e) {
              console.error('Failed to parse WebSocket message:', e);
            }
          };

          ws.onclose = () => {
            setWsStatus('disconnected');
            setWsSend(null);
            wsRef.current = null;
            if (!didOpen) {
              setWsAuthFailed(true);
              // Don't hot-loop retrying a likely auth rejection every
              // second with exponential backoff from scratch -- jump
              // straight to the slow, steady retry cadence. A reload
              // (which re-reads window.__CONDUCTOR_TOKEN__) is the actual
              // remedy; this just avoids flooding the server meanwhile.
              reconnectDelayRef.current = MAX_RECONNECT_DELAY;
            }
            scheduleReconnect();
          };

          ws.onerror = () => {
            // onclose fires after onerror
          };
        } catch {
          scheduleReconnect();
        }
      })
      .catch((err) => {
        if (abortController.signal.aborted) return;
        console.error('Failed to fetch state:', err);
        scheduleReconnect();
      });
  }, [processEvent, replayState, setWsStatus, setWsSend, setWsAuthFailed, scheduleReconnect]);

  // Keep the ref in sync with the latest connect callback
  connectRef.current = connect;

  useEffect(() => {
    connect();

    return () => {
      if (fetchAbortRef.current) {
        fetchAbortRef.current.abort();
      }
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
      }
      if (wsRef.current) {
        wsRef.current.close();
      }
      setWsSend(null);
    };
  }, [connect, setWsSend]);
}

