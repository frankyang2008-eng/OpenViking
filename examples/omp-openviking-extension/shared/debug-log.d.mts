// GENERATED FROM examples/memory-plugin-shared/lib. DO NOT EDIT.
export declare function createLogger(
  hookName: string,
  overrideCfg?: { debug?: boolean; debugLogPath?: string },
): {
  log(stage: string, data?: unknown): void;
  logError(stage: string, err: unknown): void;
};
