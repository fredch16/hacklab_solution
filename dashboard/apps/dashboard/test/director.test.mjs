import test from "node:test";
import assert from "node:assert/strict";
import * as D from "../dist/director.js";

const near = (a, b, eps = 1e-6) => a.every((x, i) => Math.abs(x - b[i]) < eps);
// The real layout: a 960x720 Station frame in a 16:9 frame with object-fit: contain -> side bars.
const contain = { width: 1600, height: 900, natW: 960, natH: 720, fit: "contain" };

test("contain: letterbox bars are outside the picture", () => {
  // picture is 1200 wide, centred: bars of 200 px each side
  assert.equal(D.toSource(contain, 100, 450), null);
  assert.ok(near(D.toSource(contain, 200, 0), [0, 0]));
  assert.ok(near(D.toSource(contain, 800, 450), [0.5, 0.5]));
  assert.ok(near(D.toSource(contain, 1400, 900), [1, 1]));
});

test("cover: the cropped part maps beyond the element", () => {
  const cover = { ...contain, fit: "cover" };     // scale 1600/960: picture 1200 high, 150 px cut top and bottom
  assert.ok(near(D.toSource(cover, 0, 0), [0, 150 / 1200]));
  assert.ok(near(D.toView(cover, 0.5, 0), [800, -150]));
});

test("round trip under rotation and mirroring", () => {
  for (const rotate of [0, 90, 180, 270]) for (const mirror of [false, true]) for (const fit of ["contain", "cover", "fill"]) {
    const d = { ...contain, fit, rotate, mirror };
    const [x, y] = D.toView(d, 0.3, 0.7);
    const back = D.toSource(d, x, y);
    if (back) assert.ok(near(back, [0.3, 0.7]), `${fit} ${rotate} ${mirror}: ${back}`);
  }
});

test("mirror flips left and right", () => {
  const d = { ...contain, mirror: true };
  assert.ok(near(D.toSource(d, 200, 450), [1, 0.5]));
});

test("resize: the same source box at two element sizes", () => {
  const box = [0.25, 0.25, 0.5, 0.75];
  for (const [w, h] of [[1600, 900], [640, 360], [500, 700]]) {
    const d = { ...contain, width: w, height: h };
    const [l, t, bw, bh] = D.boxToView(d, box);
    const again = D.dragToBox(d, l, t, l + bw, t + bh);
    assert.ok(near(again, box, 1e-9), `${w}x${h}: ${again}`);
  }
});

test("drag: reversed drags normalize, outside start refused, tiny box refused, end clipped", () => {
  assert.ok(near(D.dragToBox(contain, 1400, 900, 800, 450), [0.5, 0.5, 1, 1]));
  assert.equal(D.dragToBox(contain, 50, 50, 800, 450), null);
  assert.equal(D.dragToBox(contain, 800, 450, 805, 455), null);   // 6x6 source px < 12
  assert.ok(near(D.dragToBox(contain, 800, 450, 1590, 900), [0.5, 0.5, 1, 1]));
});

test("no picture yet: nothing maps", () => {
  assert.equal(D.toSource({ ...contain, natW: 0 }, 800, 450), null);
});
