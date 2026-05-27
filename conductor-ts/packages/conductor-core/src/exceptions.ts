/** Exception hierarchy for Conductor. Mirrors src/conductor/exceptions.py */

export class ConductorError extends Error {
  readonly suggestion?: string;
  readonly filePath?: string;
  readonly lineNumber?: number;

  constructor(
    message: string,
    opts: { suggestion?: string; filePath?: string; lineNumber?: number } = {},
  ) {
    super(message);
    this.name = this.constructor.name;
    this.suggestion = opts.suggestion;
    this.filePath = opts.filePath;
    this.lineNumber = opts.lineNumber;
  }

  get errorType(): string {
    return this.constructor.name;
  }
}

export class ConfigurationError extends ConductorError {}
export class ValidationError extends ConductorError {}
export class ExecutionError extends ConductorError {}
export class ProviderError extends ConductorError {}
export class TemplateError extends ConductorError {}
export class RouteError extends ConductorError {}
export class CheckpointError extends ConductorError {}
export class MaxIterationsError extends ConductorError {}
export class TimeoutError extends ConductorError {}
export class AgentTimeoutError extends ConductorError {}
export class InterruptError extends ConductorError {}
