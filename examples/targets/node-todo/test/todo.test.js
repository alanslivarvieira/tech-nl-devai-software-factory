import { test } from "node:test";
import assert from "node:assert/strict";
import { add, list } from "../src/todo.js";

test("adds a todo", () => {
  assert.deepEqual(add("buy milk"), { title: "buy milk", done: false });
  assert.equal(list().length, 1);
});
