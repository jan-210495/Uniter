// Strict rendered-markup DOM seam for /users; fetches are served by pytest's
// isolated, signed-in API client. Temporary passwords travel only over this
// pipe so the test can prove the displayed value is the real credential.
const vm = require('node:vm');
const readline = require('node:readline');
const lines = readline.createInterface({input: process.stdin});
const queue = [];
let waiting;
lines.on('line', line => waiting ? (waiting(JSON.parse(line)), waiting = null) : queue.push(JSON.parse(line)));
function receive() { return queue.length ? Promise.resolve(queue.shift()) : new Promise(resolve => waiting = resolve); }
function send(message) { process.stdout.write(JSON.stringify(message) + '\n'); }

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.value = ''; this.textContent = ''; this.className = '';
    this.hidden = false; this.disabled = false; this.dataset = {}; this.children = []; this.handlers = {};
  }
  addEventListener(event, handler) { this.handlers[event] = handler; }
  append(...nodes) { this.children.push(...nodes.filter(node => typeof node !== 'string')); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  focus() {}
  select() {}
  closest(selector) { return selector === 'button[data-action]' && this.tagName === 'BUTTON' && this.dataset.action ? this : null; }
  descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
}

(async () => {
  const input = await receive();
  const elements = Object.fromEntries(input.elements.map(id => [id, new Element()]));
  elements['temporary-password-panel'].hidden = true;
  const windowHandlers = {};
  const document = {getElementById: id => elements[id] || null, createElement: tag => new Element(tag), execCommand() {}};
  const window = {confirm: () => true, addEventListener: (event, handler) => { windowHandlers[event] = handler; }};
  vm.runInNewContext(input.script, {
    document, window, console, String, JSON,
    navigator: {clipboard: {writeText: async () => {}}},
    fetch: async (url, options = {}) => {
      send({request: {url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null}});
      const response = await receive();
      return {ok: response.status < 400, status: response.status, json: async () => response.body};
    },
  });

  const panel = elements['temporary-password-panel'];
  const value = elements['temporary-password-value'];
  const rows = elements['users-rows'];
  const state = () => ({
    panelHidden: panel.hidden, panelUser: elements['temporary-password-user'].textContent,
    password: value.value, createFeedback: elements['create-user-feedback'].textContent,
    feedback: elements['users-feedback'].textContent,
    rows: rows.children.map(row => ({
      id: row.dataset.userId,
      text: row.descendants().map(node => node.textContent).filter(Boolean),
      actions: row.descendants().filter(node => node.tagName === 'BUTTON').map(node => node.dataset.action),
    })),
  });

  elements['new-username'].value = '  Lina ';
  await elements['create-user-form'].handlers.submit({preventDefault() {}});
  const created = state();
  elements['temporary-password-done'].handlers.click();
  const dismissed = state();

  elements['new-username'].value = 'has space';
  await elements['create-user-form'].handlers.submit({preventDefault() {}});
  const invalid = state();

  const lina = rows.children.find(row => row.descendants().some(node => node.textContent === 'lina'));
  const resetButton = lina.descendants().find(node => node.dataset.action === 'reset');
  await rows.handlers.click({target: resetButton});
  const reset = state();
  windowHandlers.pagehide();
  const left = state();

  send({result: {created, dismissed, invalid, reset, left}});
  process.exit(0);
})();
