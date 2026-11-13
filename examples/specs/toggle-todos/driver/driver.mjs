#!/usr/bin/env node
// Tests-as-data DRIVER (JSON-lines protocol) — generated at freeze time, then hash-locked.
//
// Request per stdin line : {"id": 1, "op": "<op>", "input": {...}}
// Response per line      : {"id": 1, "output": <json>}  or  {"id": 1, "error": "<message>"}
//
// Rules: NO assertions / expected values here (the factory runner compares everything); only call the
// system's PUBLIC interface (see intent.yaml -> interface); import production code lazily inside ops;
// map domain errors to thrown Errors whose message is the user-facing error text.
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";
import path from "node:path";

// Every __reset__ starts a new "generation": modules are re-imported once per test case (fresh in-memory
// state), but stay the same across the steps of one case (state is kept between given/when/then).
let generation = 0;
const load = async (rel) =>
  import(pathToFileURL(path.resolve(process.cwd(), rel)).href + `?generation=${generation}`);

// ---- op table: DSL op name -> async (input) => JSON-serialisable output ---------------------------
const todo = () => load("src/todo.js");
const OPS = {
  add: async (input) => (await todo()).add(input.title),
  list: async () => ({ items: (await todo()).list() }),
  toggle: async (input) => (await todo()).toggle(input.title),
  remaining: async () => ({ count: (await todo()).remaining() }),
};
// Extra clean-up between cases (databases, temp files…). Module state is already reset by `generation`.
const reset = async () => {
  generation += 1;
};
// ---------------------------------------------------------------------------------------------------

const realWrite = process.stdout.write.bind(process.stdout);
console.log = (...args) => process.stderr.write(args.join(" ") + "\n");

const rl = createInterface({ input: process.stdin });
let queue = Promise.resolve();
rl.on("line", (line) => {
  queue = queue.then(async () => {
    if (!line.trim()) return;
    const { id, op, input = {} } = JSON.parse(line);
    let resp;
    try {
      if (op === "__ping__") resp = { id, output: "pong" };
      else if (op === "__reset__") { await reset(); resp = { id, output: null }; }
      else if (op in OPS) resp = { id, output: (await OPS[op](input ?? {})) ?? null };
      else resp = { id, error: `unknown op: ${op}`, error_type: "UnknownOp" };
    } catch (err) {
      resp = { id, error: String(err?.message ?? err), error_type: err?.name ?? "Error" };
    }
    realWrite(JSON.stringify(resp) + "\n");
  });
});
