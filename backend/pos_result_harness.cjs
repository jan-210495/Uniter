// Execute the rendered /pos page script against a minimal DOM double to verify
// the generated-code result panel: copy paths (async Clipboard API, legacy
// selection fallback, truthful failure), the optional dialer handoff and the
// explicit pending -> completed/failed confirmation flow.
// Clipboard behaviour is simulated per run; network calls are captured only.
const vm = require('node:vm');
const fs = require('node:fs');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const html = input.html;
const clipboardMode = input.clipboard || 'modern'; // modern | reject | none
const execCommandMode = input.execCommand || 'ok'; // ok | fail | throw | missing

class Element {
  constructor(id = '') {
    this.id = id;
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
    this.selected = false;
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
  select() { this.selected = true; }
  setSelectionRange() {}
  scrollIntoView() {}
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    return child;
  }
  contains() { return true; }
  closest() { return null; }
  querySelector() { return new Element(); }
  querySelectorAll() { return []; }
}

const elements = new Map();
let lastFocused = null;

function element(id) {
  if (!elements.has(id)) elements.set(id, new Element(id));
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

// Registered ids, their rendered value/hidden state and initial classes.
const idPattern = /<([a-zA-Z][\w-]*)([^>]*\bid="([^"]+)"[^>]*)>/g;
let idMatch;
while ((idMatch = idPattern.exec(html)) !== null) {
  const target = element(idMatch[3]);
  const attributes = parseAttributes(idMatch[2]);
  if (attributes.value !== undefined) target.value = attributes.value;
  if (attributes.hidden !== undefined) target.hidden = true;
  if (attributes.disabled !== undefined) target.disabled = true;
  (attributes.class || '').split(/\s+/).filter(Boolean).forEach((name) => target.classes.add(name));
  Object.entries(attributes)
    .filter(([name]) => name.startsWith('data-'))
    .forEach(([name, value]) => {
      const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
      target.dataset[key] = value;
    });
}

// Selects start on their first option, as a fresh page does.
function firstOptionValue(selectId) {
  const select = new RegExp(`<select[^>]*id="${selectId}"[^>]*>([\\s\\S]*?)</select>`).exec(html);
  const option = /<option value="([^"]*)"/.exec(select ? select[1] : '');
  return option ? option[1] : null;
}
for (const selectId of ['operator', 'service']) {
  const value = firstOptionValue(selectId);
  if (value !== null) element(selectId).value = value;
}

// Final-status actions (Slice 2), found by their explicit attribute.
const statusButtons = [];
const statusPattern = /<button([^>]*\bdata-status-action="[^"]*"[^>]*)>/g;
let statusMatch;
while ((statusMatch = statusPattern.exec(html)) !== null) {
  const attributes = parseAttributes(statusMatch[1]);
  const target = element(attributes.id);
  target.dataset.statusAction = attributes['data-status-action'];
  statusButtons.push(target);
}

// Voucher chips (Slice 1) with the operator of their group.
const groups = [];
const voucherButtons = [];
const groupPattern = /<div data-voucher-operator="([^"]+)"([^>]*)>([\s\S]*?)<\/div>\s*<\/div>/g;
let groupMatch;
while ((groupMatch = groupPattern.exec(html)) !== null) {
  const group = new Element();
  group.dataset.voucherOperator = groupMatch[1];
  group.hidden = /\shidden(\s|$)/.test(groupMatch[2]);
  groups.push(group);
  const buttonPattern = /<button([^>]*)>/g;
  let buttonMatch;
  while ((buttonMatch = buttonPattern.exec(groupMatch[3])) !== null) {
    const attributes = parseAttributes(buttonMatch[1]);
    if (attributes['data-amount'] === undefined) continue;
    const button = new Element();
    button.dataset.amount = attributes['data-amount'];
    button.dataset.voucherOperator = group.dataset.voucherOperator;
    button.attributes = attributes;
    (attributes.class || '').split(/\s+/).filter(Boolean).forEach((name) => button.classes.add(name));
    voucherButtons.push(button);
  }
}

const body = new Element('body');
const requests = [];
const clipboardWrites = [];
const createdHelpers = [];
let execCommandCalls = 0;
let execCommandArgs = null;

const clipboard = clipboardMode === 'none' ? undefined : {
  writeText: async (text) => {
    clipboardWrites.push(text);
    if (clipboardMode === 'reject') throw new Error('NotAllowedError');
  },
};

let execCommand;
if (execCommandMode !== 'missing') {
  execCommand = (command) => {
    execCommandCalls += 1;
    execCommandArgs = command;
    if (execCommandMode === 'throw') throw new Error('execCommand failed');
    return execCommandMode === 'ok';
  };
}

const document = {
  body,
  getElementById: element,
  querySelector(selector) {
    if (selector.startsWith('#')) return element(selector.slice(1));
    return new Element();
  },
  querySelectorAll(selector) {
    if (selector === '.voucher-button') return voucherButtons;
    if (selector === '[data-voucher-operator]') return groups;
    if (selector === '[data-status-action]') return statusButtons;
    return [];
  },
  createElement(tagName) {
    const created = new Element();
    if (String(tagName).toLowerCase() === 'textarea') createdHelpers.push(created);
    return created;
  },
  createDocumentFragment: () => new Element(),
  addEventListener() {},
  execCommand,
};

const pointerCoarse = input.mobile !== false;
const window = {
  isSecureContext: input.secureContext,
  matchMedia: (query) => ({ matches: query.includes('pointer: coarse') ? pointerCoarse : false }),
};

const context = {
  document,
  window,
  console,
  AbortController,
  Date,
  queueMicrotask,
  setTimeout: () => 0,
  clearTimeout() {},
  navigator: { clipboard },
  fetch: async (url, options = {}) => {
    const bodyData = options.body ? JSON.parse(options.body) : null;
    requests.push({ url, method: options.method || 'GET', body: bodyData });
    if (url === '/api/settings/commission') {
      const commission = input.commission || 'ok';
      if (commission === 'error') {
        return { ok: false, status: 500, json: async () => ({ detail: 'Rate unavailable' }) };
      }
      const rate = commission === 'invalid' ? 'nope' : '0.05';
      return { ok: true, status: 200, json: async () => ({ commission_rate: rate }) };
    }
    if (url === '/api/transactions' && (options.method || 'GET') === 'POST') {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          id: 42,
          reference: 'TXN-harness',
          status: 'pending',
          ussd_string: '*150*65477*0944123456*50#',
          amount_cents: 5000,
        }),
      };
    }
    if (url.startsWith('/api/transactions/') && url.endsWith('/status')) {
      const patch = input.patch || { status: 200 };
      const ok = patch.status < 400;
      const data = ok
        ? {
            id: 42,
            reference: 'TXN-harness',
            status: bodyData.status,
            ussd_string: '*150*65477*0944123456*50#',
            amount_cents: 5000,
          }
        : { detail: 'Transaction is already completed' };
      return { ok, status: patch.status, json: async () => data };
    }
    if (url.startsWith('/api/customers/search')) {
      return { ok: true, status: 200, json: async () => [] };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  },
};

vm.runInNewContext(input.script, context);

async function flush() {
  for (let index = 0; index < 12; index += 1) await Promise.resolve();
}

function voucherButton(step) {
  const button = voucherButtons.find(
    (candidate) =>
      candidate.dataset.voucherOperator === step.voucher.operator &&
      candidate.dataset.amount === step.voucher.amount
  );
  if (!button) throw new Error(`No voucher ${step.voucher.operator} ${step.voucher.amount}`);
  return button;
}

(async () => {
  await flush();
  for (const step of input.steps || []) {
    if (step.values) {
      for (const [id, value] of Object.entries(step.values)) element(id).value = value;
    }
    if (step.voucher) {
      const button = voucherButton(step);
      for (const handler of button.handlers.click || []) {
        await handler({ preventDefault() {}, target: button });
      }
      await flush();
    }
    if (step.event) {
      const target = element(step.id);
      const handlers = target.handlers[step.event] || [];
      if (!handlers.length) throw new Error(`No ${step.event} handler on ${step.id}`);
      const event = { preventDefault() {}, target };
      for (const handler of handlers) await handler(event);
      await flush();
    }
    if (step.focus === true) {
      // Mark focus explicitly when a test needs the "manual copy" affordance.
      element(step.id).focus();
    }
  }
  const badge = element('result-status-badge');
  process.stdout.write(
    JSON.stringify({
      requests,
      clipboardWrites,
      execCommandCalls,
      execCommandArgs,
      helperCount: createdHelpers.length,
      helperFields: createdHelpers
        .filter((helper) => body.children.includes(helper))
        .map((helper) => ({ value: helper.value, readonly: helper.getAttribute('readonly') })),
      bodyChildren: body.children.length,
      lastFocused: lastFocused ? lastFocused.id : null,
      elements: Object.fromEntries(
        [...elements].map(([id, entry]) => [
          id,
          {
            value: entry.value,
            text: entry.textContent,
            hidden: Boolean(entry.hidden),
            classes: [...entry.classes],
            disabled: Boolean(entry.disabled),
            focused: Boolean(entry.focused),
            selected: Boolean(entry.selected),
          },
        ])
      ),
      badge: { text: badge.textContent, classes: [...badge.classes] },
      dial: {
        hidden: Boolean(element('dial-actions').hidden),
        href: element('dial-ussd').getAttribute('href'),
      },
      vouchers: voucherButtons.map((button) => ({
        operator: button.dataset.voucherOperator,
        amount: button.dataset.amount,
        pressed: button.getAttribute('aria-pressed'),
      })),
    })
  );
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
