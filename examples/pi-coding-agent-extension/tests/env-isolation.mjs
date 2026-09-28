/**
 * Test env isolation for the `OPENVIKING_*` / `OV_*` configuration namespace.
 *
 * `shared/config-schema.mjs` documents env as the highest-priority layer —
 * above the config file, and above any `HOME` isolation. Every knob is
 * therefore readable from whatever the developer's shell happens to export: a
 * shell with `OPENVIKING_TAKEOVER=0` makes the takeover branch unreachable
 * without failing anything. Clearing a hand-maintained list of names does not
 * hold either — that list lived in two test files with different contents and
 * covered 16 of the 64 knobs in the schema. Clear the whole prefix, and put
 * back exactly what was there.
 */

const OV_ENV_PREFIXES = ["OPENVIKING_", "OV_"];

/**
 * Delete every OV-configured variable from `env` and return what was removed,
 * so `restoreOvEnv` can put it back.
 */
export function clearOvEnv(env = process.env) {
  const saved = new Map();
  for (const name of Object.keys(env)) {
    if (!OV_ENV_PREFIXES.some((prefix) => name.startsWith(prefix))) continue;
    saved.set(name, env[name]);
    delete env[name];
  }
  return saved;
}

/** Undo one `clearOvEnv` call. */
export function restoreOvEnv(saved, env = process.env) {
  for (const [name, value] of saved) env[name] = value;
}
