// tests/test_dashboard_boot.js
// Does app.js survive being executed top-to-bottom, the way a browser does it?
//
//     node tests/test_dashboard_boot.js      (from the friday/ package dir)
//
// THIS EXISTS BECAUSE `node --check` PASSED AND THE DASHBOARD WAS BLANK.
//
// A stream subscription was added partway up the file, above the
// `const STREAM_HANDLERS = {}` it reaches through onStream(). That is valid
// syntax and a temporal-dead-zone ReferenceError at runtime — thrown at load,
// before boot(), so the entire script aborts and the page renders nothing at
// all. Not a broken card: a blank dashboard, with the server answering 200 to
// every request and nothing in the log, because the failure is entirely in
// the browser.
//
// Syntax checking cannot see it and neither can testing extracted functions
// in isolation, which is how it got through. The only thing that catches it
// is running the file.
//
// The DOM stub is deliberately dumb. It is not here to test rendering — it is
// here to get execution to the last line so load-time errors surface.

const path = require('path');

const el = () => ({
  textContent: '', innerHTML: '', value: '', offsetHeight: 0,
  style: {}, dataset: {}, disabled: false,
  classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
  addEventListener() {}, appendChild() {}, removeAttribute() {}, setAttribute() {},
  querySelector: () => el(), querySelectorAll: () => [],
  content: { cloneNode: () => ({}) },
  // Reached only once a route actually renders — which offline boot() now
  // does for Today instead of stopping at "CONNECTION ERROR". Real DOM
  // methods with no interesting return value; no-ops here are enough to let
  // execution reach the last line, same as the rest of this stub.
  focus() {}, blur() {}, remove() {}, click() {},
});

global.document = {
  getElementById: el, querySelector: el, querySelectorAll: () => [],
  createElement: el, addEventListener() {},
};
global.window = { addEventListener() {}, location: { hash: '' }, focus() {} };
global.location = { hash: '', reload() {} };
global.setInterval = () => 0;
global.clearInterval = () => {};
global.setTimeout = () => 0;
global.clearTimeout = () => {};
global.fetch = () => Promise.reject(new Error('no network in this harness'));
global.EventSource = function () { return { onmessage: null, onerror: null }; };

const target = path.resolve(__dirname, '..', 'dashboard', 'static', 'app.js');

try {
  require(target);
} catch (e) {
  const where = (e.stack || '').split('\n').find((l) => l.includes('app.js'));
  console.log(`FAIL  app.js threw at load: ${e.constructor.name}: ${e.message}`);
  if (where) console.log(`      ${where.trim()}`);
  console.log('\nA load-time throw means a BLANK dashboard — boot() never runs.');
  process.exit(1);
}

console.log('ok    app.js executes top-to-bottom with no load-time error');
console.log('\nall passed');
