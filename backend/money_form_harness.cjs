// Execute the actual rendered page scripts without installing browser dependencies.
// This is a small DOM test double, not a browser: network requests are captured.
const vm = require('node:vm');
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
class Element {
  constructor(id = '') { this.id=id; this.value=''; this.textContent=''; this.dataset={}; this.handlers={}; this.children=[]; this.hidden=false; this.classList={add(){},remove(){},toggle(){},contains(){return false;}}; }
  addEventListener(type, fn) { this.handlers[type]=fn; }
  querySelector(selector) { return element(this.id+':'+selector); }
  querySelectorAll() { return []; }
  closest() { return card; }
  setAttribute() {} removeAttribute() {} focus() {} setCustomValidity(v) { this.invalid=v; }
  reportValidity() { return ![...elements.values()].some(e=>e.invalid); }
  reset() {} append(...children) { this.children.push(...children); } appendChild(c) { this.append(c); }
  replaceChildren(...children) { this.children=children; } contains() { return true; }
}
const elements = new Map();
function element(id) { if(!elements.has(id)) elements.set(id,new Element(id)); return elements.get(id); }
const card=element('card'); card.dataset.operatorCard='mtn';
const topup=element('topup'); topup.dataset.operatorCode='mtn';
const requests=[];
const document={ getElementById:element, querySelector:element, querySelectorAll(s){ return s==='[data-operator-card]'?[card]:s==='.topup-form'?[topup]:[]; }, createElement:()=>new Element(), createDocumentFragment:()=>new Element(), addEventListener(){} };
let balance=0;
const context={document, console, AbortController, Date, setTimeout:()=>0, clearTimeout(){}, window:{confirm:()=>true}, navigator:{}, alert(){}, fetch:async(url,opts={})=>{
  const body=opts.body?JSON.parse(opts.body):null; requests.push({url,method:opts.method||'GET',body});
  let data=[];
  if(url==='/api/operators/topup') { balance+=body.amount_cents; data={new_balance_cents:balance,reference:'TOPUP-test'}; }
  else if(url==='/api/operators/balance') data=[{operator_code:'mtn',balance_cents:balance}];
  else if(url==='/api/settings/commission') data={commission_rate:'0.05'};
  else if(url==='/api/transactions') data={ussd_string:'captured'};
  else if(url.includes('/balance')||url.includes('/summary')) data={total_charges_cents:50000,total_payments_cents:0,total_debt_cents:50000};
  return {ok:true,status:200,json:async()=>data};
}};
vm.runInNewContext(input.script,context);
async function flush(){for(let i=0;i<12;i++) await Promise.resolve();}
(async()=>{
 await flush();
 for(const step of input.steps){
  if(step.values) for(const [id,value] of Object.entries(step.values)) element(id).value=value;
  if(step.event){
   const target=element(step.id);
   let event={preventDefault(){},target};
   if(step.row){ const row=new Element(); row.dataset=step.row; const button=new Element(); button.dataset.action=step.action; button.closest=()=>row; event.target={closest:()=>button}; }
   if(!target.handlers[step.event]) throw new Error('No handler '+step.id+' '+step.event);
   await target.handlers[step.event](event); await flush();
  }
 }
 process.stdout.write(JSON.stringify({requests,elements:Object.fromEntries([...elements].map(([k,e])=>[k,{value:e.value,text:e.textContent}]))}));
})().catch(e=>{console.error(e);process.exit(1);});
