// Tiny in-memory todo list used to demo and end-to-end test the software factory (Node stack).
const todos = [];

export function add(title) {
  const clean = (title ?? "").trim();
  if (!clean) throw new Error("Title must not be empty");
  const todo = { title: clean, done: false };
  todos.push(todo);
  return { ...todo };
}

export function list() {
  return todos.map((t) => ({ ...t }));
}
