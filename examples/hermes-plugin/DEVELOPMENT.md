# Developing the Hermes memory provider

This directory imports the OpenViking provider from
[`NousResearch/hermes-plugin-openviking`](https://github.com/NousResearch/hermes-plugin-openviking/tree/5dca75f4d3dcef9467ce2ff32e170d84c679de5f),
commit `5dca75f4d3dcef9467ce2ff32e170d84c679de5f`.

At the initial import, `__init__.py`, `_setup.py`, and `plugin.yaml` matched that
handoff and `plugins/memory/openviking/` in Hermes Agent commit
`d177b119e9c56c9ddc0b7379ffce52341ec06584`. The original MIT license is retained
in this directory. Original contributor history is available in
[Hermes Agent](https://github.com/NousResearch/hermes-agent/commits/d177b119e9c56c9ddc0b7379ffce52341ec06584/plugins/memory/openviking).

Contributions to this directory are provided under its [MIT license](LICENSE).
Preserve the existing copyright and permission notice.

The distribution name is `hermes-plugin-openviking`. The provider, plugin, and
future Hermes catalog key remain `openviking`. Existing `memory.openviking`
settings, environment variables, linked `ovcli.conf` files, data paths, and
`viking_*` tools keep their current behavior.

The active-session commit lifecycle was ported from
[KoNit-K's Hermes PR #112533](https://github.com/NousResearch/hermes-agent/pull/112533),
with the original author retained. The OpenViking adaptation uses a configurable
pending-token threshold instead of the original six-turn trigger.

Gateway sender attribution and recall scope adapt
[Hermes PR #105812](https://github.com/NousResearch/hermes-agent/pull/105812),
including liuhao1024's capture change from
[PR #98506](https://github.com/NousResearch/hermes-agent/pull/98506), with the
original author retained. The adaptation uses Hermes's existing per-turn author
hooks and preserves the original sender-ID encoding and Personal/Shared setup
presets. Shared Agent changes gateway session settings only after confirmation;
Personal Agent preserves them. An upgrade with no recall scope set retains the
previous recall requests. No Hermes core patch is required.

## Migration coordination

After this directory is merged, submit a Hermes catalog entry with:

- `name: openviking`
- `repo: https://github.com/volcengine/OpenViking`
- `subdir: examples/hermes-plugin`
- `sha`: the full reviewed OpenViking commit SHA

Publish the catalog entry and validate migration before Hermes removes its
bundled provider. Hermes PR [#114569](https://github.com/NousResearch/hermes-agent/pull/114569)
adds catalog recovery for configured providers that no longer resolve. The
bundled provider takes precedence while it remains present.

This plugin does not add a Desktop `config_schema.py`.
The wizard uses private helpers from `hermes_cli.memory_setup`; changes to
those helpers require compatibility checks. The plugin uses HTTP and does not
install or package the OpenViking server.

## Validation

Use a Hermes checkout with its development dependencies installed. From that
checkout, run its canonical test runner against this directory:

```bash
PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}" HERMES_TEST_FILE_RETRIES=0 \
  scripts/run_tests.sh /path/to/OpenViking/examples/hermes-plugin/tests \
  --confcutdir=/path/to/OpenViking/examples/hermes-plugin/tests -q
```

`--confcutdir` keeps pytest from importing the plugin as a test package before
Hermes loads it under its own namespace. The tests use temporary profile homes
and remove bundled-provider discovery.
They check external loading across profiles, HTTP tool dispatch, and cancelled
setup without changing existing configuration. Gateway tests use mock events
through Hermes's turn hooks and memory manager. They cover sender changes,
capture retries, commits, recall scopes, compression fallback, and missing
sender metadata. Setup tests cover both presets, confirmation, cancellation,
profile-local persistence, connection routes, and actual Hermes session keys.
Run the upstream OpenViking
provider tests in Hermes as well when changing provider behavior:

```bash
HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh \
  tests/plugins/memory/test_openviking_provider.py \
  tests/plugins/memory/test_openviking_optional_peer.py \
  tests/plugins/memory/test_openviking_shutdown.py \
  tests/plugins/memory/test_openviking_endpoint_always_blocked.py \
  tests/openviking_plugin/test_openviking.py -q
```
