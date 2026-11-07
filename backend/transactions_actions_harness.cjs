// Execute the rendered /transactions page script against a minimal DOM double
// to verify the Slice 2.3 row actions: marking a successful sale failed and
// removing a failed row, including the confirmation, the server-side guard
// responses and the polite live feedback. Network calls are captured and
// answered, never sent.
const vm = require('node:vm');
const fs = require('node:fs');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const html = input.html;
const confirmMode = input.confirm || 'accept'; // accept | cancel
const patchMode = input.patch || 'ok'; // ok | conflict | missing
const deleteMode = input.delete || 'ok'; // ok | conflict | missing

class Element {
  constructor(tagName = '') {
    this.tagName = String(tagName).toLowerCase();
    this.id = '';
    this.value = '';
    this.textContent = '';
    this.dataset = {};
    this.handlers = {};
    this.attributes = {};
    this.hidden = false;
    this.disabled = false;
    this.children = [];
    this.style = {};
    this.focused = false;
    this.colSpan = 0;
    this.classes = new Set();
    this.classList = {
      add: (...names) => names.forEach((name) => this.classes.add(name)),
      remove: (...names) => names.forEach((name) => this.classes.delete(name)),
      toggle: (name, force) => {
        const on = force === undefined ? !this.classes.has(name) : Boolean(force);
        if (on) this.classes.add(name);
        else this.classes.delete(name);
        return on;
      },
      contains: (name) => this.classes.has(name),
    };
  }

  addEventListener(type, handler) {
    if (!this.handlers[type]) this.handlers[type] = [];
    this.handlers[type].push(handler);
  }

  setAttribute(name, value) { this.attributes[name.toLowerCase()] = String(value); }
  getAttribute(name) { return this.attributes[name.toLowerCase()] ?? null; }
  removeAttribute(name) { delete this.attributes[name.toLowerCase()]; }
  setCustomValidity() {}
  reportValidity() { return true; }
  focus() { this.focused = true; lastFocused = this; }
  select() {}
  setSelectionRange() {}
  scrollIntoView() {}
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; }
  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    return child;
  }
  contains() { return true; }
  closest() { return null; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}

const elements = new Map();
const buttons = [];
const allCreated = [];
let lastFocused = null;

function element(id) {
  if (!elements.has(id)) elements.set(id, new Element('div'));
  return elements.get(id);
}

function parseAttributes(tag) {
  const attributes = {};
  const pattern = /([\w:-]+)(?:="([^"]*)")?/g;
  let match;
  while ((match = pattern.exec(tag)) !== null) {
    attributes[match[1].toLowerCase()] = match[2] === undefined ? '' : match[2];
  }
  return attributes;
}

const idPattern = /<([a-zA-Z][\w-]*)([^>]*\bid="([^"]+)"[^>]*)>/g;
let idMatch;
while ((idMatch = idPattern.exec(html)) !== null) {
  const target = element(idMatch[3]);
  const attributes = parseAttributes(idMatch[2]);
  if (attributes.value !== undefined) target.value = attributes.value;
  if (attributes.hidden !== undefined) target.hidden = true;
  (attributes.class || '').split(/\s+/).filter(Boolean).forEach((name) => target.classes.add(name));
}

// The status select starts on its first option, as a fresh page does.
function firstOptionValue(selectId) {
  const select = new RegExp(`<select[^>]*id="${selectId}"[^>]*>([\\s\\S]*?)</select>`).exec(html);
  const option = /<option value="([^"]*)"/.exec(select ? select[1] : '');
  return option ? option[1] : null;
}
for (const selectId of ['filter-operator', 'filter-service', 'filter-status']) {
  const value = firstOptionValue(selectId);
  if (value !== null) element(selectId).value = value;
}

const requests = [];
let confirmCalls = 0;
let confirmMessage = null;
const confirm = (message) => {
  confirmCalls += 1;
  confirmMessage = message;
  return confirmMode === 'accept';
};

function jsonResponse(ok, status, data) {
  return { ok, status, json: async () => data };
}

const rows = JSON.parse(JSON.stringify(input.rows || []));

const document = {
  body: new Element('body'),
  getElementById: element,
  querySelector(selector) {
    if (selector.startsWith('#')) return element(selector.slice(1));
    return new Element();
  },
  querySelectorAll() { return []; },
  createElement(tagName) {
    const created = new Element(tagName);
    allCreated.push(created);
    if (created.tagName === 'button') buttons.push(created);
    return created;
  },
  createDocumentFragment: () => new Element('fragment'),
  addEventListener() {},
};

const window = { confirm };

const context = {
  document,
  window,
  console,
  AbortController,
  URLSearchParams,
  Date,
  Number,
  String,
  JSON,
  queueMicrotask,
  setTimeout: (callback) => { if (typeof callback === 'function') callback(); return 0; },
  clearTimeout() {},
  navigator: {},
  fetch: async (url, options = {}) => {
    const method = options.method || 'GET';
    requests.push({ url, method, body: options.body ? JSON.parse(options.body) : null });
    if (url.startsWith('/api/transactions') && method === 'GET') {
      return jsonResponse(true, 200, rows);
    }
    if (/^\/api\/transactions\/\d+\/status$/.test(url) && method === 'PATCH') {
      if (patchMode === 'conflict') return jsonResponse(false, 409, { detail: 'Transaction is already failed' });
      if (patchMode === 'missing') return jsonResponse(false, 404, { detail: 'Transaction not found' });
      const id = Number(url.split('/')[3]);
      const row = rows.find((candidate) => candidate.id === id);
      if (row) row.status = 'failed';
      return jsonResponse(true, 200, row || {});
    }
    if (/^\/api\/transactions\/\d+$/.test(url) && method === 'DELETE') {
      if (deleteMode === 'conflict') {
        return jsonResponse(false, 409, { detail: 'Only failed transactions can be removed' });
      }
      if (deleteMode === 'missing') return jsonResponse(false, 404, { detail: 'Transaction not found' });
      const id = Number(url.split('/')[3]);
      const index = rows.findIndex((candidate) => candidate.id === id);
      if (index >= 0) rows.splice(index, 1);
      return jsonResponse(true, 204, null);
    }
    if (url.startsWith('/api/customers/search')) return jsonResponse(true, 200, []);
    return jsonResponse(true, 200, {});
  },
};

vm.runInNewContext(input.script, context);

async function flush() {
  for (let index = 0; index < 32; index += 1) await Promise.resolve();
}

function actionButton(txId, action) {
  const row = element('tx-rows').children.find(
    (child) => child.tagName === 'tr' && child.dataset.txId === String(txId)
  );
  if (!row) throw new Error(`No rendered row for transaction ${txId}`);
  const cell = row.children[row.children.length - 1];
  const group = cell.children[0];
  const button = (group ? group.children : []).find(
    (candidate) => candidate.dataset.action === action
  );
  if (!button) throw new Error(`No ${action} button on transaction ${txId}`);
  return button;
}

(async () => {
  await flush();
  for (const step of input.steps || []) {
    if (step.clickAction) {
      const button = actionButton(step.clickAction.txId, step.clickAction.action);
      const handlers = button.handlers.click || [];
      if (!handlers.length) throw new Error('action button has no click handler');
      for (const handler of handlers) await handler({ preventDefault() {}, currentTarget: button });
      await flush();
    }
    if (step.clickTwice) {
      const button = actionButton(step.clickTwice.txId, step.clickTwice.action);
      const handlers = button.handlers.click || [];
      await Promise.all(
        handlers
          .map((handler) => handler({ preventDefault() {}, currentTarget: button }))
          .concat(
            handlers.map((handler) => handler({ preventDefault() {}, currentTarget: button }))
          )
      );
      await flush();
    }
  }

  const renderedRows = element('tx-rows').children.map((row) => ({
    id: row.dataset.txId || null,
    cells: row.children.length,
    status: (() => {
      const badge = row.children[6] && row.children[6].children[0];
      return badge ? badge.textContent : null;
    })(),
    actions: (() => {
      const cell = row.children[row.children.length - 1];
      const group = cell.children[0];
      return (group ? group.children : []).map((button) => ({
        action: button.dataset.action,
        text: button.textContent,
        label: button.getAttribute('aria-label'),
        title: button.getAttribute('title'),
        disabled: Boolean(button.disabled),
      }));
    })(),
  }));

  process.stdout.write(
    JSON.stringify({
      requests,
      rows: renderedRows,
      confirmCalls,
      confirmMessage,
      lastFocused: lastFocused ? lastFocused.id || lastFocused.tagName : null,
      elements: Object.fromEntries(
        [...elements].map(([id, entry]) => [
          id,
          {
            text: entry.textContent,
            value: entry.value,
            hidden: Boolean(entry.hidden),
            focused: Boolean(entry.focused),
            classes: [...entry.classes],
          },
        ])
      ),
      buttons: buttons.map((button) => ({
        action: button.dataset.action || null,
        text: button.textContent,
        label: button.getAttribute('aria-label'),
        disabled: Boolean(button.disabled),
      })),
      emptyCells: element('tx-rows').children
        .filter((row) => row.children.length === 1)
        .map((row) => ({ colSpan: row.children[0].colSpan, text: row.children[0].textContent })),
    })
  );
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
