import { useEffect, useRef, useCallback } from 'react';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { WorkflowEvent } from '@/types/events';

const MAX_RECONNECT_DELAY = 30000;

export function useWebSocket() {
  const processEvent = useWorkflowStore((s) => s.processEvent);
  const replayState = useWorkflowStore((s) => s.replayState);
  const setWsStatus = useWorkflowStore((s) => s.setWsStatus);
  const setWsSend = useWorkflowStore((s) => s.setWsSend);

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
      .then((resp) => resp.json())
      .then((events: WorkflowEvent[]) => {
        if (events && events.length > 0) {
          replayState(events);
        }

        const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${proto}//${window.location.host}/ws`;

        try {
          const ws = new WebSocket(wsUrl);
          wsRef.current = ws;

          ws.onopen = () => {
            reconnectDelayRef.current = 1000;
            setWsStatus('connected');
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
  }, [processEvent, replayState, setWsStatus, setWsSend, scheduleReconnect]);

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
