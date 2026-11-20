// AIRUX Snake: the game's rules, with no drawing and no timers, so they can be unit-tested.
// Works in the page (window.SnakeCore) and in Node (module.exports).
(function (root) {
  "use strict";

  const DIRS = { up: { x: 0, y: -1 }, down: { x: 0, y: 1 }, left: { x: -1, y: 0 }, right: { x: 1, y: 0 } };
  const OPPOSITE = { up: "down", down: "up", left: "right", right: "left" };
  // milliseconds per move for speeds 1 (relaxed) to 5 (fast)
  const SPEEDS = [190, 145, 110, 85, 65];

  // a seeded random number generator, so tests (and replays) are repeatable
  function rng(seed) {
    let s = seed >>> 0 || 1;
    return function () {
      s ^= s << 13; s >>>= 0; s ^= s >> 17; s ^= s << 5; s >>>= 0;
      return s / 4294967296;
    };
  }

  function createGame(opts) {
    const o = Object.assign({ cols: 24, rows: 18, mode: "walls", speed: 3, random: Math.random, length: 4 }, opts || {});
    const y = Math.floor(o.rows / 2);
    const x0 = Math.floor(o.cols / 2);
    const snake = [];
    for (let i = 0; i < o.length; i++) snake.push({ x: x0 - i, y });   // head first, moving right
    const g = {
      cols: o.cols, rows: o.rows, mode: o.mode === "wrap" ? "wrap" : "walls",
      speed: Math.min(5, Math.max(1, o.speed | 0)), random: o.random,
      snake, dir: "right", queue: [], food: null, score: 0, eaten: 0, grow: 0,
      alive: true, won: false, steps: 0,
    };
    placeFood(g);
    return g;
  }

  function interval(g) { return SPEEDS[g.speed - 1]; }

  // up to two turns are remembered, so a quick "up, left" between two moves is not lost; a turn
  // straight back into the snake's own neck is ignored
  function turn(g, dir) {
    if (!DIRS[dir] || !g.alive) return false;
    const last = g.queue.length ? g.queue[g.queue.length - 1] : g.dir;
    if (dir === last || dir === OPPOSITE[last] || g.queue.length >= 2) return false;
    g.queue.push(dir);
    return true;
  }

  function occupied(g, x, y) { return g.snake.some((p) => p.x === x && p.y === y); }

  function placeFood(g) {
    const free = [];
    for (let y = 0; y < g.rows; y++) for (let x = 0; x < g.cols; x++) if (!occupied(g, x, y)) free.push({ x, y });
    if (!free.length) { g.food = null; g.won = true; g.alive = false; return null; }
    g.food = free[Math.floor(g.random() * free.length)];
    return g.food;
  }

  // one move; returns what happened: "moved", "ate", "crashed" or "won"
  function step(g) {
    if (!g.alive) return g.won ? "won" : "crashed";
    if (g.queue.length) g.dir = g.queue.shift();
    const d = DIRS[g.dir];
    let x = g.snake[0].x + d.x, y = g.snake[0].y + d.y;
    if (g.mode === "wrap") {
      x = (x + g.cols) % g.cols; y = (y + g.rows) % g.rows;
    } else if (x < 0 || y < 0 || x >= g.cols || y >= g.rows) {
      g.alive = false; return "crashed";
    }
    const eating = g.food && g.food.x === x && g.food.y === y;
    // the tail moves out of the way this turn unless the snake is growing
    const body = g.grow > 0 || eating ? g.snake : g.snake.slice(0, -1);
    if (body.some((p) => p.x === x && p.y === y)) { g.alive = false; return "crashed"; }
    g.snake.unshift({ x, y });
    g.steps++;
    if (eating) {
      g.eaten++;
      g.score += g.speed * (g.mode === "walls" ? 2 : 1);   // braver settings score more
      g.grow += 1;
    }
    if (g.grow > 0) g.grow--; else g.snake.pop();
    if (eating) { placeFood(g); if (g.won) return "won"; return "ate"; }
    return "moved";
  }

  const api = { DIRS, OPPOSITE, SPEEDS, rng, createGame, interval, turn, step, placeFood };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.SnakeCore = api;
})(typeof window !== "undefined" ? window : globalThis);
