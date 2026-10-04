import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import crypto from 'node:crypto';
import { EventEmitter } from 'node:events';
import { Readable } from 'node:stream';

const source = fs.readFileSync(new URL('../api/push.js', import.meta.url), 'utf8');
const receipt = '00000000-0000-4000-8000-000000000001';
function harness(statuses = [200]) {
  const sent = [];
  const http2 = { connect() {
    const session = new EventEmitter();
    session.close = () => {}; session.destroy = () => {};
    session.request = headers => {
      const req = new EventEmitter();
      req.end = body => {
        sent.push({headers, body: JSON.parse(body)});
        queueMicrotask(() => { req.emit('response', {':status': statuses.shift() ?? 200}); req.emit('end'); });
      };
      return req;
    };
    return session;
  }};
  const context = {
    module: { exports: null }, require: name => name === 'node:http2' ? http2 : {...crypto, sign: () => Buffer.from('signed')},
    process: {env: {PUSH_SECRET:'secret', APNS_KEY_P8:'key', APNS_KEY_ID:'id', APNS_TEAM_ID:'team', APNS_TOPIC:'topic', KV_REST_API_URL:'https://redis', KV_REST_API_TOKEN:'redis'}},
    Buffer, URL, setTimeout, clearTimeout, fetch: async () => ({ok:true, json:async () => [1,1,1,1].map(result => ({result}))}),
  };
  vm.runInNewContext(source, context);
  return { sent, async run(extra = {}, auth = 'Bearer secret') {
    const req = Readable.from([Buffer.from(JSON.stringify({tok:'a'.repeat(64), env:'prod', title:'Dark Army needs you', badge:2, kind:'question', ...extra}))]);
    req.method='POST'; req.url='/api/push?ch='+'b'.repeat(32); req.headers={authorization:auth};
    const res={statusCode:0, setHeader(){}, end(text){this.text=text;}};
    await context.module.exports(req,res);
    return res;
  }};
}
test('receipt survives sanitizer through actual Apple request; the shaped session_id rides as the subject, no arbitrary metadata', async () => {
  const h=harness(); assert.equal((await h.run({destination_version:1, receipt_id:receipt, question:'private', root:'/secret', session_id:'s', sound:'malicious'})).statusCode,200);
  assert.deepEqual(Object.keys(h.sent[0].body).sort(),['aps','destination_version','receipt_id','session_id']);
  assert.equal(h.sent[0].body.session_id,'s');
  assert.equal(h.sent[0].body.aps.sound,'buzz-question.wav');
  assert.equal(h.sent[0].headers['apns-collapse-id'],receipt);
  assert(!JSON.stringify(h.sent[0]).includes('private'));
});
test('unrelated receipts do not collapse, retries reuse same receipt',async()=>{
  const h=harness([403,200,200]); await h.run({destination_version:1,receipt_id:receipt});
  await h.run({destination_version:1,receipt_id:receipt.replace(/1$/,'2')});
  assert.equal(h.sent[0].headers['apns-collapse-id'],h.sent[1].headers['apns-collapse-id']);
  assert.notEqual(h.sent[1].headers['apns-collapse-id'],h.sent[2].headers['apns-collapse-id']);
});
test('legacy payload retains generic collapse and sound policy',async()=>{
  const h=harness(); await h.run({kind:'permission'});
  assert.equal(h.sent[0].headers['apns-collapse-id'],'bob');
  assert.equal(h.sent[0].body.aps.sound,'buzz-permission.wav');
  assert.equal(h.sent[0].body.receipt_id,undefined);
  // No subject sent, no subject key: byte-identical to before the subject existed.
  assert.equal(h.sent[0].body.session_id,undefined); assert.equal(h.sent[0].body.card_id,undefined);
  assert.deepEqual(Object.keys(h.sent[0].body),['aps']);
});

// --- the subject: which agent and which card the buzz is about ---------------------

test('a card_id rides beside the session id and adds no other key',async()=>{
  const h=harness(); assert.equal((await h.run({session_id:'s-1', card_id:'card-9'})).statusCode,200);
  const {body}=h.sent[0];
  assert.deepEqual(Object.keys(body).sort(),['aps','card_id','session_id']);
  assert.equal(body.session_id,'s-1'); assert.equal(body.card_id,'card-9');
  assert.deepEqual(Object.keys(body.aps).sort(),['alert','badge','sound','thread-id']);
  const only=harness(); assert.equal((await only.run({card_id:'card-9'})).statusCode,200);
  assert.deepEqual(Object.keys(only.sent[0].body).sort(),['aps','card_id']);
});
test('a bad-shape session_id or card_id is 400 bad subject and nothing is sent',async()=>{
  for (const fields of [{session_id:'bad id!'},{card_id:'bad id!'},{session_id:'s-1',card_id:'x'.repeat(121)},{session_id:'',card_id:''},{session_id:'s-1',card_id:{}}]) {
    const h=harness(); const res=await h.run(fields);
    assert.equal(res.statusCode,400,JSON.stringify(fields)); assert.equal(res.text,'bad subject'); assert.equal(h.sent.length,0);
  }
});
test('a subject with no act sets no category and no act keys',async()=>{
  const h=harness(); assert.equal((await h.run({session_id:'s-1', card_id:'card-9', destination_version:1, receipt_id:receipt})).statusCode,200);
  const {body}=h.sent[0];
  assert.equal(body.aps.category,undefined); assert.equal(body.act,undefined); assert.equal(body.request_id,undefined);
  assert.deepEqual(Object.keys(body).sort(),['aps','card_id','destination_version','receipt_id','session_id']);
});
test('the act leg still names its category over the same session id',async()=>{
  const h=harness(); assert.equal((await h.run({session_id:'s-1', card_id:'card-9', act:'acknowledge'})).statusCode,200);
  const {body}=h.sent[0];
  assert.equal(body.aps.category,'bob.acknowledge'); assert.equal(body.act,'acknowledge'); assert.equal(body.session_id,'s-1'); assert.equal(body.card_id,'card-9');
  const bad=harness(); assert.equal((await bad.run({session_id:'bad id!', act:'acknowledge'})).statusCode,400); assert.equal(bad.sent.length,0);
});
test('a review act carries the run id alone, no session',async()=>{
  const h=harness(); assert.equal((await h.run({act:'review', run_id:'r-1'})).statusCode,200);
  const {body}=h.sent[0];
  assert.equal(body.aps.category,'bob.review'); assert.equal(body.act,'review'); assert.equal(body.run_id,'r-1');
  assert.equal('session_id' in body,false); assert.equal('request_id' in body,false);
  const bad=harness(); assert.equal((await bad.run({act:'review', run_id:'bad id!'})).statusCode,400); assert.equal(bad.sent.length,0);
  const none=harness(); assert.equal((await none.run({act:'review'})).statusCode,400); assert.equal(none.sent.length,0);
});
test('the subject block is the alert leg alone: the activity leg is untouched',async()=>{
  const h=harness(); assert.equal((await h.run({title:undefined,badge:undefined,kind:undefined,...state, card_id:'card-9'})).statusCode,200);
  assert.deepEqual(Object.keys(h.sent[0].body),['aps']);
  assert.equal(JSON.stringify(h.sent[0]).includes('card-9'),false);
});
test('bad version/UUID and absent credential never reach Apple',async()=>{
  for(const fields of [{destination_version:2,receipt_id:receipt},{destination_version:1,receipt_id:'bad'},{receipt_id:receipt}]) {
    const h=harness(); assert.equal((await h.run(fields)).statusCode,400); assert.equal(h.sent.length,0);
  }
  const h=harness(); assert.equal((await h.run({},'Bearer wrong')).statusCode,401); assert.equal(h.sent.length,0);
});
test('the work line becomes the banner subtitle and adds no other key',async()=>{
  const h=harness(); assert.equal((await h.run({work:'Rename the strip ladder'})).statusCode,200);
  const aps=h.sent[0].body.aps;
  assert.equal(aps.alert.subtitle,'Rename the strip ladder');
  assert.deepEqual(Object.keys(aps).sort(),['alert','badge','sound','thread-id']);
  assert.deepEqual(Object.keys(aps.alert).sort(),['subtitle','title']);
});
test('an absent or blank work line leaves the subtitle undefined',async()=>{
  for(const fields of [{},{work:''},{work:'   '}]) {
    const h=harness(); assert.equal((await h.run(fields)).statusCode,200);
    const aps=h.sent[0].body.aps;
    assert.equal(aps.alert.subtitle,undefined);
    assert.deepEqual(Object.keys(aps.alert),['title']);
  }
});
test('an unbounded work line arrives clamped at eighty characters',async()=>{
  const h=harness(); await h.run({work:'x'.repeat(500)});
  assert.equal(h.sent[0].body.aps.alert.subtitle.length,80);
});
test('work without a title is still a bad request',async()=>{
  const h=harness(); assert.equal((await h.run({title:undefined,work:'Rename the strip ladder'})).statusCode,400);
  assert.equal(h.sent.length,0);
});
test('the need line becomes the banner body under the title and subtitle',async()=>{
  const h=harness(); assert.equal((await h.run({title:'Vex wants to run a tool',work:'Rename the strip ladder',need:'Approve running Bash'})).statusCode,200);
  const {body}=h.sent[0];
  assert.deepEqual(Object.keys(body),['aps']);
  assert.deepEqual(body.aps.alert,{title:'Vex wants to run a tool',subtitle:'Rename the strip ladder',body:'Approve running Bash'});
  assert.deepEqual(Object.keys(body.aps).sort(),['alert','badge','sound','thread-id']);
  assert.equal(body.need,undefined);
});
test('an absent or blank need line sends no body key at all',async()=>{
  const h=harness(); await h.run({work:'Rename the strip ladder'});
  const today=JSON.stringify(h.sent[0].body);
  for(const fields of [{work:'Rename the strip ladder',need:''},{work:'Rename the strip ladder',need:'   '}]) {
    const g=harness(); assert.equal((await g.run(fields)).statusCode,200);
    assert.equal('body' in g.sent[0].body.aps.alert,false);
    assert.equal(JSON.stringify(g.sent[0].body),today);
  }
});
test('an unbounded need line arrives clamped at one hundred and twenty characters',async()=>{
  const h=harness(); await h.run({need:'x'.repeat(300)});
  assert.equal(h.sent[0].body.aps.alert.body.length,120);
});
test('the clamp counts code points, so a need ending in an emoji lands whole',async()=>{
  // 119 code points plus one emoji: exactly the Mac's clamp, and the last
  // character is an astral one that a UTF-16 slice would have split.
  const need='x'.repeat(118)+'\u2026'+'\u{1F600}';
  const h=harness(); await h.run({need});
  const body=h.sent[0].body.aps.alert.body;
  assert.equal(body,need);
  assert.equal(Array.from(body).length,120);
  assert(body.endsWith('\u{1F600}'));
});

// --- the live card's leg -----------------------------------------------------------

const state = {event:'update', nickname:'Vex', slug:'vex', kind:'permission', work:'Rename the strip ladder', since:1758355200, session_id:'s-1'};
function activity(h, extra = {}) {
  // No `title` on this leg: `run` adds one by default, so it is dropped here.
  return h.run({title:undefined, badge:undefined, kind:undefined, ...state, ...extra});
}
test('an update is a liveactivity push on the activity topic with exactly the seven state keys',async()=>{
  const h=harness(); assert.equal((await activity(h)).statusCode,200);
  const {headers,body}=h.sent[0];
  assert.equal(headers['apns-push-type'],'liveactivity');
  assert.equal(headers['apns-topic'],'topic.push-type.liveactivity');
  assert.equal(headers['apns-priority'],'10');
  assert.equal(headers['apns-collapse-id'],undefined);
  assert.deepEqual(Object.keys(body),['aps']);
  assert.equal(body.aps.event,'update');
  assert.equal(typeof body.aps.timestamp,'number');
  assert.equal(body.aps['stale-date'],body.aps.timestamp+1800);
  assert.equal(body.aps['dismissal-date'],undefined);
  assert.deepEqual(Object.keys(body.aps['content-state']).sort(),['kind','nickname','session_id','since','slug','updated_at','work']);
  assert.deepEqual(body.aps['content-state'],{nickname:'Vex',slug:'vex',kind:'permission',work:'Rename the strip ladder',since:1758355200,session_id:'s-1',updated_at:body.aps.timestamp});
  assert.equal(body.aps.alert,undefined); assert.equal(body.aps.sound,undefined);
});
test('extra caller fields never reach Apple on the activity leg',async()=>{
  const h=harness(); assert.equal((await activity(h,{question:'private', root:'/secret', sound:'malicious', title:'Dark Army needs you', act:'permission', request_id:'r1'})).statusCode,200);
  const wire=JSON.stringify(h.sent[0]);
  for (const word of ['private','/secret','malicious','Dark Army needs you','request_id','category']) assert(!wire.includes(word), word);
  assert.deepEqual(Object.keys(h.sent[0].body),['aps']);
});
test('an end carries a dismissal date and the last state, kind optional',async()=>{
  const h=harness(); assert.equal((await activity(h,{event:'end'})).statusCode,200);
  assert.equal(h.sent[0].body.aps.event,'end');
  assert.equal(h.sent[0].body.aps['dismissal-date'],h.sent[0].body.aps.timestamp);
  const bare=harness(); assert.equal((await bare.run({title:undefined,badge:undefined,kind:undefined,event:'end'})).statusCode,200);
  assert.deepEqual(bare.sent[0].body.aps['content-state'],{nickname:'',slug:'',kind:'attention',work:'',since:0,session_id:'',updated_at:bare.sent[0].body.aps.timestamp});
});
test('an unknown event, kind, slug shape or session id is 400 and nothing is sent',async()=>{
  for (const fields of [{event:'start'},{event:'refresh'},{kind:'security'},{kind:'finished'},{slug:'Vex'},{slug:'x'.repeat(25)},{session_id:'bad id!'},{since:'soon'},{since:-1},{event:'update',since:undefined}]) {
    const h=harness(); assert.equal((await activity(h,fields)).statusCode,400,JSON.stringify(fields)); assert.equal(h.sent.length,0);
  }
});
test('a picks buzz plays the question cue and a picks update carries run_id as the eighth key',async()=>{
  const b=harness(); assert.equal((await b.run({kind:'picks'})).statusCode,200);
  assert.equal(b.sent[0].body.aps.sound,'buzz-question.wav');
  const h=harness(); assert.equal((await activity(h,{kind:'picks',run_id:'r-1'})).statusCode,200);
  const content=h.sent[0].body.aps['content-state'];
  assert.deepEqual(Object.keys(content).sort(),['kind','nickname','run_id','session_id','since','slug','updated_at','work']);
  assert.equal(content.kind,'picks'); assert.equal(content.run_id,'r-1');
  const plain=harness(); assert.equal((await activity(plain,{kind:'picks'})).statusCode,200);
  assert.equal(plain.sent[0].body.aps['content-state'].run_id,undefined);
  assert.equal(Object.keys(plain.sent[0].body.aps['content-state']).length,7);
});
test('a run_id out of shape is 400 and nothing is sent',async()=>{
  const h=harness(); assert.equal((await activity(h,{kind:'picks',run_id:'bad id!'})).statusCode,400); assert.equal(h.sent.length,0);
});
test('the activity work line is clamped and the wrong secret is refused before any parse',async()=>{
  const h=harness(); await activity(h,{work:'x'.repeat(500)});
  assert.equal(h.sent[0].body.aps['content-state'].work.length,80);
  const w=harness(); assert.equal((await w.run({...state,title:undefined},'Bearer wrong')).statusCode,401); assert.equal(w.sent.length,0);
});
test('the alert leg is untouched by the activity leg: type, topic, collapse id and body',async()=>{
  const h=harness(); assert.equal((await h.run({work:'Rename the strip ladder'})).statusCode,200);
  const {headers,body}=h.sent[0];
  assert.equal(headers['apns-push-type'],'alert');
  assert.equal(headers['apns-topic'],'topic');
  assert.equal(headers['apns-collapse-id'],'bob');
  assert.deepEqual(Object.keys(body),['aps']);
  assert.deepEqual(Object.keys(body.aps).sort(),['alert','badge','sound','thread-id']);
  assert.equal(body.aps.event,undefined); assert.equal(body.aps['content-state'],undefined);
});
test('apple 5xx and 429 on the activity leg are 503 unavailable; a 4xx stays 502 apple refused',async()=>{
  for (const [apple, route, word] of [[503,503,'unavailable'],[500,503,'unavailable'],[429,503,'unavailable'],[400,502,'apple refused'],[410,502,'apple refused']]) {
    const h=harness([apple]); const res=await activity(h,{event:'end'});
    assert.equal(res.statusCode,route,`apple ${apple}`); assert.equal(res.text,word,`apple ${apple}`); assert.equal(h.sent.length,1);
  }
});
test('an update with the five fleet fields reaches Apple with exactly the twelve content-state keys',async()=>{
  const h=harness();
  const res=await activity(h,{working:1, needs_you:0, standing_by:2, cost_usd:1.234, tokens_k:45});
  assert.equal(res.statusCode,200);
  assert.equal(res.text,'ok shape=2');
  const content=h.sent[0].body.aps['content-state'];
  assert.deepEqual(Object.keys(content).sort(),['cost_usd','kind','needs_you','nickname','session_id','since','slug','standing_by','tokens_k','updated_at','work','working']);
  assert.equal(content.cost_usd,1.23);
  assert.equal(content.working,1);
  assert.equal(content.needs_you,0);
  assert.equal(content.standing_by,2);
  assert.equal(content.tokens_k,45);
  assert.equal(content.nickname,'Vex');
});
test('the two per-hour rates ride beside the totals, rounded like them, and a bad one is 400',async()=>{
  const h=harness();
  const res=await activity(h,{working:1, cost_usd:1, tokens_k:2, cost_usd_hour:4.216, tokens_k_hour:1100});
  assert.equal(res.statusCode,200);
  const content=h.sent[0].body.aps['content-state'];
  assert.equal(content.cost_usd_hour,4.22);
  assert.equal(content.tokens_k_hour,1100);
  for (const fields of [{cost_usd_hour:-1},{cost_usd_hour:'4'},{tokens_k_hour:1.5},{tokens_k_hour:1000000}]) {
    const bad=harness();
    assert.equal((await activity(bad,fields)).statusCode,400,JSON.stringify(fields));
    assert.equal(bad.sent.length,0);
  }
});
test('an update without the fleet fields is still exactly the six content-state keys',async()=>{
  const h=harness(); assert.equal((await activity(h)).statusCode,200);
  const content=h.sent[0].body.aps['content-state'];
  assert.deepEqual(Object.keys(content).sort(),['kind','nickname','session_id','since','slug','updated_at','work']);
  assert.equal(content.working,undefined);
  assert.equal(content.cost_usd,undefined);
});
test('a negative count, a non-integer token figure, a cost over the cap or a string is 400 and nothing is sent',async()=>{
  for (const fields of [{working:-1},{tokens_k:1.5},{cost_usd:1000001},{cost_usd:'1.00'},{standing_by:'none'}]) {
    const h=harness();
    const res=await activity(h,fields);
    assert.equal(res.statusCode,400,JSON.stringify(fields));
    assert.equal(res.text,'bad activity');
    assert.equal(h.sent.length,0);
  }
});
test('an end carrying the five fleet fields keeps them on the content-state',async()=>{
  const h=harness();
  assert.equal((await activity(h,{event:'end', working:0, needs_you:1, standing_by:0, cost_usd:0.5, tokens_k:2})).statusCode,200);
  const content=h.sent[0].body.aps['content-state'];
  assert.equal(h.sent[0].body.aps.event,'end');
  assert.equal(content.needs_you,1);
  assert.equal(content.cost_usd,0.5);
  assert.equal(content.tokens_k,2);
});
test('a zero cost is kept and an int above the cap is 400 with nothing sent',async()=>{
  const h=harness();
  assert.equal((await activity(h,{cost_usd:0, working:0})).statusCode,200);
  assert.equal(h.sent[0].body.aps['content-state'].cost_usd,0);
  assert.equal(h.sent[0].body.aps['content-state'].working,0);
  const bad=harness();
  const res=await activity(bad,{tokens_k:1000000});
  assert.equal(res.statusCode,400);
  assert.equal(res.text,'bad activity');
  assert.equal(bad.sent.length,0);
});
test('the activity leg answers ok shape=2 and the alert leg still answers ok',async()=>{
  const live=harness(); assert.equal((await activity(live)).text,'ok shape=2');
  const alert=harness(); const res=await alert.run({work:'Rename the strip ladder'});
  assert.equal(res.statusCode,200);
  assert.equal(res.text,'ok');
  assert.equal(alert.sent[0].headers['apns-push-type'],'alert');
});
// One agent's question carries that agent's face. `vex` is on identity.NAMES.
// The plain-language example `dom` is not, after the 22 Sep rebrand, so it
// sets neither key — the same rule as `mrrobot`. `ptys` is kept: the live
// card's slug shape rejects it, and this list does not.
test('a question with a roster face sets mutable-content 1 and top-level face', async () => {
  const h = harness();
  assert.equal((await h.run({face: 'vex'})).statusCode, 200);
  assert.equal(h.sent[0].body.face, 'vex');
  assert.equal(h.sent[0].body.aps['mutable-content'], 1);
});
test('ptys is kept as a face', async () => {
  const h = harness();
  assert.equal((await h.run({face: 'ptys'})).statusCode, 200);
  assert.equal(h.sent[0].body.face, 'ptys');
  assert.equal(h.sent[0].body.aps['mutable-content'], 1);
});
test('a name that is not an agent sets neither mutable-content nor face', async () => {
  for (const face of ['../x', 'mrrobot', 'dom']) {
    const h = harness();
    assert.equal((await h.run({face})).statusCode, 200, face);
    assert.equal(h.sent[0].body.face, undefined, face);
    assert.equal(h.sent[0].body.aps['mutable-content'], undefined, face);
    assert.equal(h.sent.length, 1, face);
  }
  const missing = harness();
  assert.equal((await missing.run({})).statusCode, 200);
  assert.equal(missing.sent[0].body.face, undefined);
  assert.equal(missing.sent[0].body.aps['mutable-content'], undefined);
});
test('an activity event still has no mutable-content and no face', async () => {
  const h = harness();
  assert.equal((await activity(h, {face: 'vex'})).statusCode, 200);
  assert.equal(h.sent[0].body.face, undefined);
  assert.equal(h.sent[0].body.aps['mutable-content'], undefined);
  assert.deepEqual(Object.keys(h.sent[0].body), ['aps']);
});
test('the alert leg still answers 502 apple refused for every apple non-200, 5xx and 429 included',async()=>{
  for (const apple of [400,410,429,500,503]) {
    const h=harness([apple]); const res=await h.run({});
    assert.equal(res.statusCode,502,`apple ${apple}`); assert.equal(res.text,'apple refused',`apple ${apple}`);
  }
});

test('the live card keeps a non-ASCII cast face and still refuses a stranger',async()=>{
  const h=harness(); assert.equal((await activity(h,{nickname:'Ptys', slug:'ptys'})).statusCode,200);
  assert.equal(h.sent[0].body.aps['content-state'].slug,'ptys');
  const bad=harness(); assert.equal((await activity(bad,{slug:'stranger'})).statusCode,400);
  assert.equal(bad.sent.length,0);
});
