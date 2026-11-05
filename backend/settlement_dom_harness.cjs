// Strict rendered-markup DOM seam; fetches are served by pytest's isolated API client.
const vm = require('node:vm');
const readline = require('node:readline');
const lines = readline.createInterface({input: process.stdin});
const queue = [];
let waiting;
lines.on('line', line => waiting ? (waiting(JSON.parse(line)), waiting = null) : queue.push(JSON.parse(line)));
function receive() { return queue.length ? Promise.resolve(queue.shift()) : new Promise(resolve => waiting = resolve); }
class Element {
  constructor(classes = '') {
    this.value = ''; this.textContent = ''; this.innerHTML = ''; this.children = []; this.handlers = {};
    const tokens = new Set(classes.split(/\s+/));
    this.classList = {add: c => tokens.add(c), remove: c => tokens.delete(c), contains: c => tokens.has(c)};
  }
  addEventListener(event, handler) { this.handlers[event] = handler; }
  appendChild(child) { this.children.push(child); }
}
(async () => {
  const input = await receive();
  const elements = Object.fromEntries(Object.entries(input.elements).map(([id, classes]) => [id, new Element(classes)]));
  const tbody = input.ledgerBody ? new Element() : null;
  const alerts = [];
  const document = {
    getElementById: id => elements[id] || null,
    querySelector: selector => selector === '#ledger-container tbody' ? tbody : null,
    createElement: () => new Element(),
  };
  vm.runInNewContext(input.script, {document, Date, console, setTimeout() {}, alert: message => alerts.push(message),
    fetch: async (url, options = {}) => {
      process.stdout.write(JSON.stringify({request: {url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null}}) + '\n');
      const response = await receive();
      return {ok: response.status < 400, json: async () => response.body, text: async () => JSON.stringify(response.body)};
    },
  });
  elements['customer-phone'].value = '0944123456';
  elements['payment-amount'].value = '200';
  await elements['payment-form'].handlers.submit({preventDefault() {}});
  process.stdout.write(JSON.stringify({result: {
    alerts,
    debt: elements['total-debt-cents'].textContent,
    payments: elements['total-payments-cents'].textContent,
    charges: elements['total-charges-cents'].textContent,
    summaryHidden: elements['debt-summary'].classList.contains('hidden'),
    ledgerRows: tbody ? tbody.children.length : null,
  }}) + '\n');
  lines.close();
})().catch(error => {console.error(error); process.exit(1);});
