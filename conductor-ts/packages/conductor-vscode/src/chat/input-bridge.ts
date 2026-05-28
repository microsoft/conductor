/**
 * Input bridge — suspends the workflow engine while waiting for
 * the user's next chat turn to supply an answer.
 *
 * The workflow engine calls `requestInput(req)` which returns a Promise.
 * The bridge queues the request and resolves the promise when
 * `resume(answer)` is called from the chat participant on the next turn.
 */
import type { UserInputRequest, UserInputResponse } from "@conductor/core";

interface PendingRequest {
  request: UserInputRequest;
  resolve: (response: UserInputResponse) => void;
  reject: (err: Error) => void;
}

export interface InputBridge {
  /** Called by the WorkflowEngine when it needs user input. */
  requestInput: (req: UserInputRequest) => Promise<UserInputResponse>;
  /** Resume the pending request with an answer from the next chat turn. */
  resume: (answer: string) => void;
  /** Async iterable of pending requests (for the chat participant to consume). */
  requests: AsyncIterable<UserInputRequest>;
  /** Close and reject any pending request. */
  close: () => void;
  /**
   * One-shot Promise that resolves with the next request when requestInput is
   * called, or null when the bridge is closed.  Create a fresh call per race.
   */
  nextRequest: () => Promise<UserInputRequest | null>;
}

export function createInputBridge(): InputBridge {
  const queue: PendingRequest[] = [];
  let nextResolve: ((req: UserInputRequest) => void) | undefined;
  let nextRequestResolve: ((req: UserInputRequest | null) => void) | undefined;
  let closed = false;

  const requestInput = (req: UserInputRequest): Promise<UserInputResponse> => {
    return new Promise<UserInputResponse>((resolve, reject) => {
      if (closed) {
        reject(new Error("InputBridge is closed"));
        return;
      }
      const pending: PendingRequest = { request: req, resolve, reject };
      queue.push(pending);
      nextResolve?.(req);
      nextResolve = undefined;
      nextRequestResolve?.(req);
      nextRequestResolve = undefined;
    });
  };

  const resume = (answer: string): void => {
    const pending = queue.shift();
    if (pending) {
      const wasFreeform = !pending.request.choices?.includes(answer);
      pending.resolve({ answer, wasFreeform });
    }
  };

  const close = (): void => {
    closed = true;
    nextRequestResolve?.(null);
    nextRequestResolve = undefined;
    for (const pending of queue) {
      pending.reject(new Error("InputBridge closed"));
    }
    queue.length = 0;
  };

  const nextRequest = (): Promise<UserInputRequest | null> => {
    if (queue.length > 0) return Promise.resolve(queue[0]!.request);
    if (closed) return Promise.resolve(null);
    return new Promise<UserInputRequest | null>((resolve) => {
      nextRequestResolve = resolve;
    });
  };

  // Async iterable for the chat participant to await new requests
  const requests: AsyncIterable<UserInputRequest> = {
    [Symbol.asyncIterator]() {
      return {
        async next(): Promise<IteratorResult<UserInputRequest>> {
          if (closed && queue.length === 0) {
            return { done: true, value: undefined as unknown as UserInputRequest };
          }
          if (queue.length > 0) {
            return { done: false, value: queue[0]!.request };
          }
          return new Promise<IteratorResult<UserInputRequest>>((resolve) => {
            nextResolve = (req) => resolve({ done: false, value: req });
          });
        },
      };
    },
  };

  return { requestInput, resume, requests, close, nextRequest };
}
