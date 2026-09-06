// Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: AGPL-3.0

import { loadAgentHookConfig } from "../../memory-plugin-shared/lib/agent-hook-runtime.mjs";

export const CLIENT_ID = "agy";
export const SESSION_PREFIX = "agy-";

export function loadAgyConfig() {
  return loadAgentHookConfig(CLIENT_ID);
}
