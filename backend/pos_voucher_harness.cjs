// Execute the rendered /pos page script against a minimal DOM double so the
// voucher behaviour can be asserted without installing browser dependencies.
// The document is rebuilt from the rendered markup, network calls are captured
// and never sent, and every step is driven through the page's real handlers.
const vm = require('node:vm');
const fs = require('node:fs');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const html = input.html;

const elements = new Map();

function parseAttributes(tag) {
  const attributes = {};
  const pattern = /([\w:-]+)(?:="([^"]*)")?/g;
  let match;
  while ((match = pattern.exec(tag)) !== null) {
    const name = match[1].toLowerCase();
    attributes[name] = match[2] === undefined ? '' : match[2];
  }
  return attributes;
}

class Element {
  constructor(id = '') {
    this.id = id;
    this.value = '';
    this.textContent = '';
    this.dataset = {};
    this.handlers = {};
    this.attributes = {};
    this.hidden = false;
    this.children = [];
    this.invalid = '';
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
  setCustomValidity(message) { this.invalid = message || ''; }
  reportValidity() { return ![...elements.values()].some((entry) => entry.invalid); }
  focus() {}
  select() {}
  scrollIntoView() {}
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); }
  replaceChildren(...children) { this.children = children; }
  contains() { return true; }
  closest() { return null; }
  querySelector() { return new Element(); }
  querySelectorAll() { return []; }
}

function element(id) {
  if (!elements.has(id)) elements.set(id, new Element(id));
  return elements.get(id);
}

// --- Build the element registry from the rendered markup -------------------
const idPattern = /<([a-zA-Z][\w-]*)([^>]*\bid="([^"]+)"[^>]*)>/g;
let idMatch;
while ((idMatch = idPattern.exec(html)) !== null) {
  const target = element(idMatch[3]);
  const attributes = parseAttributes(idMatch[2]);
  if (attributes.value !== undefined) target.value = attributes.value;
  if (attributes.hidden !== undefined) target.hidden = true;
  if (attributes.type !== undefined) target.type = attributes.type;
}

function firstOptionValue(selectId) {
  const select = new RegExp(`<select[^>]*id="${selectId}"[^>]*>([\\s\\S]*?)</select>`).exec(html);
  const option = /<option value="([^"]*)"/.exec(select ? select[1] : '');
  return option ? option[1] : '';
}
element('operator').value = firstOptionValue('operator');
element('service').value = firstOptionValue('service');

// --- Voucher groups and buttons -------------------------------------------
const groups = [];
const buttons = [];
const groupPattern = /<div data-voucher-operator="([^"]+)"([^>]*)>([\s\S]*?)<\/div>\s*<\/div>/g;
let groupMatch;
while ((groupMatch = groupPattern.exec(html)) !== null) {
  const group = new Element();
  group.dataset.voucherOperator = groupMatch[1];
  group.attributes = parseAttributes(groupMatch[2]);
  group.hidden = /\shidden(\s|$)/.test(groupMatch[2]);
  const label = /aria-label="([^"]*)"/.exec(groupMatch[2]);
  group.label = label ? label[1] : '';
  const buttonPattern = /<button([^>]*)>/g;
  let buttonMatch;
  while ((buttonMatch = buttonPattern.exec(groupMatch[3])) !== null) {
    const button = new Element();
    button.attributes = parseAttributes(buttonMatch[1]);
    button.dataset.amount = button.attributes['data-amount'];
    button.dataset.voucherOperator = group.dataset.voucherOperator;
    button.textContent = button.dataset.amount;
    const classes = button.attributes.class || '';
    classes.split(/\s+/).filter(Boolean).forEach((name) => button.classes.add(name));
    button.group = group;
    buttons.push(button);
  }
  groups.push(group);
}

const document = {
  getElementById: element,
  querySelector(selector) {
    if (selector.startsWith('#')) return element(selector.slice(1));
    return new Element();
  },
  querySelectorAll(selector) {
    if (selector === '.voucher-button') return buttons;
    if (selector === '[data-voucher-operator]') return groups;
    return [];
  },
  createElement: () => new Element(),
  createDocumentFragment: () => new Element(),
  addEventListener() {},
};

const requests = [];
const context = {
  document,
  console,
  AbortController,
  Date,
  queueMicrotask,
  setTimeout: () => 0,
  clearTimeout() {},
  window: { confirm: () => true },
  navigator: {},
  fetch: async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    requests.push({ url, method: options.method || 'GET', body });
    let data = {};
    if (url === '/api/settings/commission') data = { commission_rate: '0.05' };
    else if (url === '/api/transactions') data = { ussd_string: '*stub*' };
    else if (url.startsWith('/api/customers/search')) data = [];
    return { ok: true, status: 200, json: async () => data };
  },
};

vm.runInNewContext(input.script, context);

async function flush() {
  for (let index = 0; index < 12; index += 1) await Promise.resolve();
}

(async () => {
  await flush();
  for (const step of input.steps) {
    if (step.values) {
      for (const [id, value] of Object.entries(step.values)) element(id).value = value;
    }
    if (step.voucher) {
      const button = buttons.find(
        (candidate) =>
          candidate.dataset.voucherOperator === step.voucher.operator &&
          candidate.dataset.amount === step.voucher.amount
      );
      if (!button) throw new Error(`No voucher ${step.voucher.operator} ${step.voucher.amount}`);
      const event = { preventDefault() {}, target: button };
      for (const handler of button.handlers.click || []) await handler(event);
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
  }
  const fieldset = element('voucher-fieldset');
  process.stdout.write(
    JSON.stringify({
      requests,
      elements: Object.fromEntries(
        [...elements].map(([id, entry]) => [
          id,
          {
            value: entry.value,
            text: entry.textContent,
            hidden: Boolean(entry.hidden),
            invalid: entry.invalid,
          },
        ])
      ),
      voucherFieldsetHidden: Boolean(fieldset.hidden),
      groups: groups.map((group) => ({
        operator: group.dataset.voucherOperator,
        label: group.label,
        hidden: Boolean(group.hidden),
      })),
      vouchers: buttons.map((button) => ({
        operator: button.dataset.voucherOperator,
        amount: button.dataset.amount,
        pressed: button.getAttribute('aria-pressed'),
        classes: [...button.classes],
        visible: !fieldset.hidden && !button.group.hidden,
      })),
    })
  );
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
