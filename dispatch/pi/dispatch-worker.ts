// The `deliver` tool: how a dispatched pi worker hands its answer back.
//
// A pi lane without `--write` runs on the tool allowlist `read, grep, find, ls,
// deliver`, so the worker can run no command and touch no file. `deliver` is the
// one exception dispatch grants it, and it writes exactly one path: the run's
// own answer file, which dispatch names in `DISPATCH_ANSWER_FILE` before the CLI
// starts. The worker chooses the text and never the destination.
//
// dispatch loads this file with `pi -e <path>` on every pi lane, write lanes
// included, and it is shipped inside the dispatch package so an install from a
// wheel has it. A pi started outside dispatch has no answer file to write, and
// the tool says so rather than guessing at a path.

import { writeFileSync } from "node:fs";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "deliver",
    label: "Deliver",
    description:
      "Write the final answer to the dispatch run's answer file. Only what is " +
      "written here is captured, and this is the only file this worker can write.",
    promptSnippet: "Hand the final answer back to dispatch with deliver",
    parameters: Type.Object({
      text: Type.String({ description: "The final answer, in full" }),
    }),
    async execute(_toolCallId, params) {
      const target = process.env.DISPATCH_ANSWER_FILE;
      if (!target) {
        throw new Error(
          "DISPATCH_ANSWER_FILE is not set: this pi is not running under dispatch, " +
            "and deliver has no answer file to write",
        );
      }
      writeFileSync(target, params.text, "utf8");
      return {
        content: [
          {
            type: "text",
            text: `Delivered ${params.text.length} characters to ${target}`,
          },
        ],
        details: {},
      };
    },
  });
}
