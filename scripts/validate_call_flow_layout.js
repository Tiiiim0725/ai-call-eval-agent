// Deterministic frontend check for the seven-stage call-flow coordinate rules.
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'frontend', 'app.js'), 'utf8');
const start = source.indexOf('const CALL_FLOW_PHASES');
const end = source.indexOf('function renderNodeLayoutEditor');
if (start < 0 || end <= start) throw new Error('call-flow layout seam missing');
const sandbox = {};
vm.runInNewContext(`${source.slice(start, end)}\nglobalThis.callFlowLayoutForTest = callFlowLayout;`, sandbox);
const layout = sandbox.callFlowLayoutForTest;

const nodes = [
  { id: 'show:a', sourceId: 'a' }, { id: 'show:b', sourceId: 'b' },
  { id: 'show:c', sourceId: 'c' }, { id: 'show:d', sourceId: 'd' },
  { id: 'show:e', sourceId: 'e' },
];
const edges = [
  { id: 'show:ab', sourceId: 'ab', source: 'show:a', target: 'show:b' },
  { id: 'show:ac', sourceId: 'ac', source: 'show:a', target: 'show:c' },
  { id: 'show:bd', sourceId: 'bd', source: 'show:b', target: 'show:d' },
  { id: 'show:cd', sourceId: 'cd', source: 'show:c', target: 'show:d' },
  { id: 'show:da', sourceId: 'da', source: 'show:d', target: 'show:a' },
];
const profile = {
  status: 'ready',
  phases: [
    { phase_id: 'pre_call', label: '外呼前准备', order: 1 },
    { phase_id: 'connect_permission', label: '接通与身份许可', order: 2 },
    { phase_id: 'needs_matching', label: '需求澄清与机会匹配', order: 5 },
  ],
  node_annotations: {
    a: { phase_id: 'pre_call' }, b: { phase_id: 'connect_permission' },
    c: { phase_id: 'connect_permission' }, d: { phase_id: 'needs_matching' },
    e: { phase_id: 'unassigned' },
  },
  edge_annotations: {
    ab: { route_tendency: 'resistant' }, ac: { route_tendency: 'receptive' },
    bd: { route_tendency: 'resistant' }, cd: { route_tendency: 'receptive' },
    da: { route_tendency: 'unknown' },
  },
};
const first = layout(nodes, edges, profile);
const second = layout(nodes, edges, profile);
const p = id => first.positions.get(`show:${id}`);
if (!(p('a').y < p('b').y && p('b').y < p('d').y && p('d').y < p('e').y)) throw new Error('phase Y order failed');
if (!(p('b').x < p('a').x && p('a').x < p('c').x)) throw new Error('local resistant/receptive branch direction failed');
if (!(p('b').x < p('d').x && p('d').x < p('c').x)) throw new Error('mixed incoming directions must remain between their parents');
if (!first.backEdgeIds.has('show:da')) throw new Error('back edge not marked');
if (first.guides.some(guide => guide.data.guide === 'lane')) throw new Error('global lane guides must not be rendered');
if (first.edgeRoutes.get('show:ab').curveDistance === first.edgeRoutes.get('show:ac').curveDistance) throw new Error('sibling routes must not share one curve track');
if (JSON.stringify([...first.positions]) !== JSON.stringify([...second.positions])) throw new Error('layout is not deterministic');
if (new Set([...first.positions.values()].map(value => `${value.x}:${value.y}`)).size !== nodes.length) throw new Error('node overlap');
const restored = layout(nodes, edges, { ...profile, manual_positions: { a: { x: 777.25, y: 888.5 } } });
if (restored.positions.get('show:a').x !== 777.25 || restored.positions.get('show:a').y !== 888.5) throw new Error('saved manual position was not restored');

// A busy phase may use multiple sub-rows and must keep node rectangles apart.
const denseNodes = Array.from({ length: 8 }, (_, index) => ({ id: `dense:${index}`, sourceId: `dense-${index}` }));
const denseProfile = {
  status: 'ready',
  phases: [{ phase_id: 'intent_objection', label: '意愿识别与异议处理', order: 4 }],
  node_annotations: Object.fromEntries(denseNodes.map((_, index) => [`dense-${index}`, { phase_id: 'intent_objection' }])),
  edge_annotations: {},
};
const dense = layout(denseNodes, [], denseProfile);
const denseY = new Set(denseNodes.map(node => dense.positions.get(node.id).y));
if (denseY.size <= 1) throw new Error('dense phase did not use vertical breathing room');
for (let left = 0; left < denseNodes.length; left += 1) {
  for (let right = left + 1; right < denseNodes.length; right += 1) {
    const a = dense.positions.get(denseNodes[left].id);
    const b = dense.positions.get(denseNodes[right].id);
    if (Math.abs(a.x - b.x) < 180 && Math.abs(a.y - b.y) < 70) throw new Error('dense phase contains overlapping nodes');
  }
}
if (dense.width < 1400) throw new Error('dense graph did not preserve a readable canvas width');
console.log(JSON.stringify({ status: 'PASS', contract: 'call-flow-relative-force-v0.48' }));
