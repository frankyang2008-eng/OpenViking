export declare const EXTENSION_VERSION: string;
export declare const OMP_CONSUMED_KNOBS: Set<string>;
export declare function detectHarness(): string;
export declare function readOmpConfigJson(extensionDir: string): Record<string, any>;
export declare function collectInertKnobs(config: Record<string, any>): string[];
export declare function loadOmpConfig(
  extensionDir: string,
  options?: { env?: Record<string, string | undefined>; cwd?: string },
): Record<string, any>;
