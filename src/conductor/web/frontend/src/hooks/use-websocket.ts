import { useEffect, useRef, useCallback } from 'react';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { WorkflowEvent } from '@/types/events';

const MAX_RECONNECT_DELAY = 30000;

export function useWebSocket() {
  const processEvent = useWorkflowStore((s) => s.processEvent);
  const replayState = useWorkflowStore((s) => s.replayState);
  const setWsStatus = useWorkflowStore((s) => s.setWsStatus);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectDelayRef = useRef(1000);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = useCallback(() => {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${proto}//${window.location.host}/ws`;

    try {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        reconnectDelayRef.current = 1000;
        setWsStatus('connected');
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
        wsRef.current = null;
        scheduleReconnect();
      };

      ws.onerror = () => {
        // onclose fires after onerror
      };
    } catch {
      scheduleReconnect();
    }
  }, [processEvent, setWsStatus]);

  const scheduleReconnect = useCallback(() => {
    setWsStatus('reconnecting');
    reconnectTimerRef.current = setTimeout(() => {
      reconnectDelayRef.current = Math.min(
        reconnectDelayRef.current * 2,
        MAX_RECONNECT_DELAY,
      );
      connect();
    }, reconnectDelayRef.current);
  }, [connect, setWsStatus]);

  useEffect(() => {
    // Fetch existing state for late-joiners, then connect
    setWsStatus('connecting');

    fetch('/api/state')
      .then((resp) => resp.json())
      .then((events: WorkflowEvent[]) => {
        if (events && events.length > 0) {
          replayState(events);
        }
        connect();
      })
      .catch((err) => {
        console.error('Failed to fetch state:', err);
        connect();
      });

    return () => {
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
      }
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, [connect, replayState, setWsStatus]);
}
