"use strict";
// AIRUX Snake rules: run with `node --test games/snake/test` (or Electron's Node in build_deb.sh).
const test = require("node:test");
const assert = require("node:assert/strict");
const S = require("../snake-core.js");

const fixed = (...values) => { let i = 0; return () => values[Math.min(i++, values.length - 1)]; };

test("a new game: snake of 4 in the middle, heading right, food on a free cell", () => {
  const g = S.createGame({ cols: 10, rows: 8, random: S.rng(7) });
  assert.deepEqual(g.snake[0], { x: 5, y: 4 });
  assert.equal(g.snake.length, 4);
  assert.equal(g.dir, "right");
  assert.ok(g.food && !g.snake.some((p) => p.x === g.food.x && p.y === g.food.y));
});

test("moving keeps the length; eating grows by one and scores", () => {
  const g = S.createGame({ cols: 10, rows: 8, speed: 3, random: S.rng(1) });
  g.food = { x: 6, y: 4 };                       // right in front of the head
  assert.equal(S.step(g), "ate");
  assert.equal(g.snake.length, 5);
  assert.equal(g.score, 6);                      // speed 3 x2 for walls
  assert.equal(S.step(g), "moved");
  assert.equal(g.snake.length, 5);
});

test("a turn straight back is ignored; two quick turns are both kept", () => {
  const g = S.createGame({ random: S.rng(3) });
  assert.equal(S.turn(g, "left"), false);
  assert.equal(S.turn(g, "up"), true);
  assert.equal(S.turn(g, "left"), true);
  assert.equal(S.turn(g, "down"), false);       // queue holds two at most
  S.step(g); assert.equal(g.dir, "up");
  S.step(g); assert.equal(g.dir, "left");
});

test("walls mode: leaving the board ends the game", () => {
  const g = S.createGame({ cols: 8, rows: 6, random: S.rng(2) });
  g.food = { x: 0, y: 0 };
  let r;
  for (let i = 0; i < 10 && g.alive; i++) r = S.step(g);
  assert.equal(r, "crashed");
  assert.equal(g.alive, false);
  assert.equal(S.step(g), "crashed");           // nothing moves after the end
});

test("wrap mode: the snake comes back on the other side", () => {
  const g = S.createGame({ cols: 8, rows: 6, mode: "wrap", random: S.rng(2) });
  g.food = { x: 0, y: 0 };
  for (let i = 0; i < 4; i++) S.step(g);
  assert.equal(g.alive, true);
  assert.equal(g.snake[0].x, 0);
});

test("running into itself ends the game, but chasing its own tail does not", () => {
  const g = S.createGame({ cols: 10, rows: 10, length: 5, random: S.rng(4) });
  g.food = { x: 0, y: 0 };
  S.turn(g, "up"); S.step(g);
  S.turn(g, "left"); S.step(g);
  S.turn(g, "down"); assert.equal(S.step(g), "crashed");

  // a 4-long snake going round a 2x2 square follows its tail into the cell it just left
  const h = S.createGame({ cols: 10, rows: 10, length: 4, random: S.rng(4) });
  h.food = { x: 0, y: 0 };
  h.snake = [{ x: 5, y: 5 }, { x: 5, y: 6 }, { x: 6, y: 6 }, { x: 6, y: 5 }];
  h.dir = "up"; h.queue = [];
  S.turn(h, "right");
  assert.equal(S.step(h), "moved");
  assert.equal(h.alive, true);
});

test("filling the whole board wins", () => {
  const g = S.createGame({ cols: 3, rows: 1, length: 2, random: fixed(0) });
  g.snake = [{ x: 1, y: 0 }, { x: 0, y: 0 }];
  g.food = { x: 2, y: 0 };
  assert.equal(S.step(g), "won");
  assert.equal(g.won, true);
});

test("speeds go from relaxed to fast; the seeded generator repeats", () => {
  assert.ok(S.SPEEDS.every((ms, i) => i === 0 || ms < S.SPEEDS[i - 1]));
  assert.equal(S.interval(S.createGame({ speed: 9 })), S.SPEEDS[4]);
  const a = S.rng(42), b = S.rng(42);
  assert.deepEqual([a(), a(), a()], [b(), b(), b()]);
});
