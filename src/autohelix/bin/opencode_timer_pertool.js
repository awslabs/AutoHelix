// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// AutoHelix opencode plugin: surface remaining iteration time after EVERY tool call.
//
// This is the opencode analog of the Claude Code PostToolUse time-left hook
// (claude_hook_time_left.sh). opencode has no shell PostToolUse hook, but it
// exposes a JS plugin hook `tool.execute.after(input, output)` (opencode >= 1.17)
// where `output.output` is the tool-result string the model reads back.
// Appending to it injects the time-remaining line into the model's context after
// each tool runs — the same idea, and the same wording, as the Claude hook.
//
// AutoHelix drops this file into <worktree>/.opencode/plugin/ for an iteration
// when budget.iteration_time is set, and exports these env vars to the agent:
//   AUTOHELIX_TIME_LEFT_SCRIPT  path to time_left.sh (the shared, tiered message)
//   AUTOHELIX_PROJECT           main project dir (used to locate the audit log)
//
// Single source of truth: we shell out to time_left.sh rather than reimplement
// the tiers, so opencode and Claude inject identical text.
//
// Safety: never throws out of the hook — a plugin error must not kill the run.
// On any failure we leave output.output untouched.

import { execFileSync } from "child_process"
import fs from "fs"
import path from "path"

function auditLogPath() {
  if (process.env.AUTOHELIX_HOOK_AUDIT_LOG) return process.env.AUTOHELIX_HOOK_AUDIT_LOG
  const project = process.env.AUTOHELIX_PROJECT
  if (project) {
    try {
      const dir = path.join(project, ".autohelix", "logs")
      fs.mkdirSync(dir, { recursive: true })
      return path.join(dir, "opencode_plugin_audit.log")
    } catch (e) {
      /* fall through */
    }
  }
  return null
}

function audit(line) {
  const log = auditLogPath()
  if (!log) return
  try {
    fs.appendFileSync(log, new Date().toISOString() + " opencode_timer: " + line + "\n")
  } catch (e) {
    /* never let logging kill the run */
  }
}

export const AutoHelixTimer = async () => {
  return {
    "tool.execute.after": async (input, output) => {
      try {
        const script = process.env.AUTOHELIX_TIME_LEFT_SCRIPT
        if (!script || typeof output.output !== "string") return
        const msg = execFileSync("bash", [script], { encoding: "utf8", timeout: 5000 }).trim()
        if (msg) {
          output.output = output.output + "\n\n[" + msg + "]"
          audit("injected: " + msg)
        }
      } catch (e) {
        audit("error: " + (e && e.message ? e.message : String(e)))
        /* never let the timer kill the run */
      }
    },
  }
}
