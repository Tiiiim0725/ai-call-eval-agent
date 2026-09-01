const API = 'http://127.0.0.1:8898/api';
const root = document.getElementById('app-view');
const titles = {
  workspace: ['工作台', '访谈证据、策略 Graph 与评价 Prompt 的受控编译链'],
  tasks: ['任务与专家', '导入访谈、确认目标专家与精确基线，再启动完整增量学习'],
  evidence: ['证据审核', '接受、改类、待确认或保留冲突，再通过 G2'],
  knowledge: ['Graph 审查', '在一张图上核对增量结构、专家原话、上下文与时间位置'],
  release: ['Prompt 与发布', '从已批准 Graph 编译双 Prompt，经 G5 后发布'],
  audit: ['审计与设置', '检查不可变操作记录与受控 LLM 配置']
};

const state = {
  view: 'workspace', tasks: [], currentTask: null, config: {},
  evidence: [], knowledge: [], knowledgeAll: [], baselines: [], gates: [], sources: [],
  selectedEvidence: null, selectedKnowledge: null,
  selectedEvidenceIds: new Set(), logs: [], hiddenDuplicateKnowledge: 0, possibleDuplicateKnowledge: 0,
  compilations: [], releases: [], selectedCompilation: null, selectedRelease: null,
  graphMode: 'candidate', graphLayout: 'call_flow', selectedGraphItem: null,
  graphBaselineId: null, graphCandidateId: null, graphLayoutProfile: null,
  scriptWorkspaces: new Map()
};

function esc(value) {
  return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function short(value, length = 72) {
  const text = String(value ?? '');
  return text.length > length ? text.slice(0, length) + '...' : text;
}
function errorText(result) {
  if (!result) return '未知错误';
  const detail = result.detail || {};
  return [result.error, result.message, detail.message, result.gate_id && `需要 ${result.gate_id}`]
    .filter(Boolean).join(' · ');
}
function badge(text, tone = '') { return `<span class="badge ${tone ? `badge-${tone}` : ''}">${esc(text)}</span>`; }
function now() { return new Date().toTimeString().slice(0, 8); }
function log(message, type = 'info') {
  state.logs.unshift({ time: now(), message, type });
  state.logs = state.logs.slice(0, 60);
  const logNode = document.getElementById('view-log');
  if (logNode) logNode.outerHTML = renderLog();
}
function renderLog() {
  const rows = state.logs.filter(item => !item.view || item.view === state.view).slice(0, 12);
  return `<div class="log" id="view-log">${rows.length ? rows.map(item =>
    `<div class="log-line log-${esc(item.type)}"><time>${item.time}</time><span>${esc(item.type.toUpperCase())}</span><span>${esc(item.message)}</span></div>`
  ).join('') : '<div class="empty">本次会话暂无操作记录</div>'}</div>`;
}

async function api(path, options = {}) {
  try {
    const response = await fetch(API + path, options);
    let data;
    try { data = await response.json(); } catch (_) { data = { error: 'invalid_json' }; }
    if (!response.ok && !data.error) data.error = `http_${response.status}`;
    data.http_status = response.status;
    return data;
  } catch (error) {
    return { error: 'network_error', message: error.message };
  }
}
function post(path, body) {
  return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json; charset=utf-8' }, body: JSON.stringify(body) });
}

function approvedGate(gateId, targetId = null) {
  return state.gates.some(g => g.gate_id === gateId && g.decision === 'approved' && (!targetId || g.target_object_id === targetId));
}
function currentTaskId() { return state.currentTask?.task_id || ''; }
function currentSourceId() { return state.currentTask?.source_id || ''; }
function taskLabel(task) { return `${task?.filename || ''}${task?.rerun_of_task_id ? '（重跑）' : ''}`; }
function currentGateLabel() {
  return state.currentTask?.current_gate || 'G0';
}
function lastCompletedGateLabel() {
  const order = ['G1', 'G2', 'G3', 'G4', 'G5'];
  const current = currentGateLabel();
  if (current === 'published') return 'G5';
  const index = order.indexOf(current);
  return index > 0 ? order[index - 1] : 'G0';
}
function setNotice(message = '', tone = 'amber') {
  const node = document.getElementById('notice');
  node.hidden = !message;
  node.className = `notice notice-${tone}`;
  node.textContent = message;
}

async function refreshBase() {
  const [health, tasks, config] = await Promise.all([api('/health'), api('/tasks'), api('/llm-config')]);
  const status = document.getElementById('connection-status');
  if (health.error) {
    status.innerHTML = '<span class="status-dot status-red"></span><span>后端未连接</span>';
    setNotice(`无法连接后端：${errorText(health)}`, 'red');
  } else {
    status.innerHTML = '<span class="status-dot status-green"></span><span>后端已连接</span>';
    setNotice('');
  }
  state.tasks = tasks.tasks || [];
  state.config = config.error ? {} : config;
  if (!state.currentTask && state.tasks.length) state.currentTask = state.tasks[0];
  if (state.currentTask) state.currentTask = state.tasks.find(t => t.task_id === state.currentTask.task_id) || state.tasks[0];
  await refreshTaskData();
  updateContext();
}

async function refreshTaskData() {
  if (!state.currentTask) {
    state.evidence = []; state.knowledge = []; state.knowledgeAll = []; state.baselines = []; state.gates = []; state.sources = []; state.hiddenDuplicateKnowledge = 0; state.possibleDuplicateKnowledge = 0;
    return;
  }
  const taskId = encodeURIComponent(currentTaskId());
  const sourceId = encodeURIComponent(currentSourceId());
  const [evidence, knowledge, knowledgeAll, baselines, gates, sources] = await Promise.all([
    api(`/evidence?source_id=${sourceId}`), api(`/knowledge?task_id=${taskId}`), api(`/knowledge?task_id=${taskId}&include_archived=1`),
    api(`/graph-baselines?task_id=${taskId}`), api(`/gates?task_id=${taskId}`), api(`/sources?task_id=${taskId}`)
  ]);
  state.evidence = evidence.evidence || [];
  state.knowledge = knowledge.knowledge || [];
  state.knowledgeAll = knowledgeAll.knowledge || state.knowledge;
  state.baselines = baselines.baselines || [];
  state.hiddenDuplicateKnowledge = knowledge.archived_duplicates || 0;
  state.possibleDuplicateKnowledge = knowledge.possible_duplicates || 0;
  state.gates = gates.gates || [];
  state.sources = sources.sources || [];
  const graphCandidates = state.knowledge
    .filter(item => item.type === 'graph' && item.status !== 'archived')
    .sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')));
  const previousGraphId = state.graphCandidateId;
  state.graphCandidateId = graphCandidates.some(item => item.object_id === state.graphCandidateId)
    ? state.graphCandidateId
    : (graphCandidates.at(-1)?.object_id || null);
  if (state.graphCandidateId !== previousGraphId) state.graphLayoutProfile = null;
  const boundBaselineId = state.currentTask?.baseline_id || state.sources[0]?.baseline_id || null;
  const candidateBaselineId = graphCandidates.find(item => item.object_id === state.graphCandidateId)?.linkage?.baseline_id;
  state.graphBaselineId = state.baselines.some(item => item.baseline_id === (candidateBaselineId || state.graphBaselineId))
    ? (candidateBaselineId || state.graphBaselineId)
    : boundBaselineId;
  if (state.selectedEvidence) state.selectedEvidence = state.evidence.find(e => e.evidence_id === state.selectedEvidence.evidence_id) || null;
  if (state.selectedKnowledge) state.selectedKnowledge = state.knowledge.find(k => k.object_id === state.selectedKnowledge.object_id) || null;
}

function updateContext() {
  const switcher = document.getElementById('task-switcher');
  switcher.innerHTML = state.tasks.length ? state.tasks.map(task =>
    `<option value="${esc(task.task_id)}" ${state.currentTask?.task_id === task.task_id ? 'selected' : ''}>${esc(taskLabel(task))} · ${esc(task.task_id)}</option>`
  ).join('') : '<option value="">暂无任务</option>';
  document.getElementById('context-expert').textContent = state.currentTask?.target_expert || '未确认';
  document.getElementById('context-gate').textContent = currentGateLabel();
  document.getElementById('context-model').textContent = state.config.model || '--';
}

function viewHeading(actions = '') {
  const [title, subtitle] = titles[state.view];
  return `<div class="view-heading"><div><h2>${title}</h2><p>${subtitle}</p></div><div class="actions">${actions}</div></div>`;
}
function switchView(view) {
  state.view = view;
  document.querySelectorAll('.nav-item').forEach(item => item.classList.toggle('is-active', item.dataset.view === view));
  document.getElementById('page-title').textContent = titles[view][0];
  document.getElementById('page-subtitle').textContent = titles[view][1];
  render();
}
function go(view) { switchView(view); }

function renderWorkspace() {
  const candidates = state.knowledge.filter(k => k.status === 'candidate').length;
  const approved = state.knowledge.filter(k => k.status === 'approved').length;
  const pendingEvidence = state.evidence.filter(e => e.status !== 'approved').length;
  const approvedGraph = state.knowledge.some(k => k.type === 'graph' && k.status === 'approved');
  const gateOrder = ['G1', 'G2', 'G3', 'G4', 'G5'];
  const currentGate = currentGateLabel();
  const currentIndex = currentGate === 'published' ? gateOrder.length : gateOrder.indexOf(currentGate);
  const gateSteps = gateOrder.map((gate, index) => {
    const done = index < currentIndex || currentGate === 'published';
    const current = index === currentIndex;
    return `<div class="gate-step ${done ? 'is-done' : current ? 'is-current' : ''}"><b>${gate}</b><small>${done ? '已通过' : current ? '待处理' : '未开始'}</small></div>`;
  }).join('');
  const issues = [];
  if (!approvedGate('G1')) issues.push('先确认目标专家并通过 G1');
  const candidateGraph = state.knowledge.some(k => k.type === 'graph' && k.status === 'candidate');
  if (!candidateGraph && !approvedGraph) issues.push('尚未运行完整访谈增量学习');
  if (!approvedGraph) issues.push('需要在 Graph 页面审核一张完整候选图');
  if (!approvedGate('G5')) issues.push('发布前需要 G5');
  return `${viewHeading('<button class="button button-primary" id="workspace-next">处理下一项</button>')}
    <div class="metric-grid">
      <div class="metric"><span>访谈任务</span><strong>${state.tasks.length}</strong><small>${esc(state.currentTask?.filename || '尚未导入')}</small></div>
      <div class="metric"><span>访谈发言</span><strong>${state.evidence.length}</strong><small>${pendingEvidence} 条由后台随 Graph 审核处理</small></div>
      <div class="metric"><span>候选知识</span><strong>${candidates}</strong><small>${approved} 个已批准</small></div>
      <div class="metric"><span>发布状态</span><strong>${approvedGate('G5') ? 'READY' : 'BLOCK'}</strong><small>${approvedGraph ? 'Graph 已批准' : 'Graph 未批准'}</small></div>
    </div>
    <div class="layout">
      <div class="panel"><div class="panel-title"><h3>Gate 状态链</h3>${badge(`当前待办 ${currentGateLabel()}`, 'blue')} ${badge(`最近完成 ${lastCompletedGateLabel()}`)}</div><div class="pipeline">${gateSteps}</div>
        <div class="panel-title" style="margin-top:18px"><h3>当前阻断</h3></div>${issues.length ? issues.map(x => `<div class="relation">${esc(x)}</div>`).join('') : '<p class="success-note">当前任务已具备发布条件。</p>'}</div>
      <div class="panel"><div class="panel-title"><h3>任务摘要</h3>${badge(state.currentTask?.status || '无任务')}</div>
        ${state.currentTask ? `<dl class="detail-list"><div><dt>task_id</dt><dd class="mono">${esc(currentTaskId())}</dd></div><div><dt>来源</dt><dd>${esc(state.currentTask.filename)}</dd></div><div><dt>目标专家</dt><dd>${esc(state.currentTask.target_expert || '未确认')}</dd></div><div><dt>证据</dt><dd>${state.evidence.length}</dd></div><div><dt>知识对象</dt><dd>${state.knowledge.length}</dd></div></dl>` : '<div class="empty">请先导入访谈文件</div>'}
        <div class="actions"><button class="button" data-go="tasks">任务与专家</button><button class="button" data-go="knowledge">审核 Graph</button></div></div>
    </div>${renderLog()}`;
}

function renderTasks() {
  const g1 = approvedGate('G1');
  const source = state.sources[0];
  return `${viewHeading('<button class="button" id="refresh-tasks">刷新</button>')}
    <div class="layout">
      <div>
        <div class="panel"><div class="panel-title"><h3>已导入任务</h3>${badge(`${state.tasks.length} 个`, 'blue')}</div>
          <div class="table-wrap"><table class="data-table"><thead><tr><th style="width:31%">任务</th><th>文件</th><th style="width:13%">Gate</th><th style="width:80px">操作</th></tr></thead><tbody>${state.tasks.map(task => `<tr data-id="${esc(task.task_id)}" class="${task.task_id === currentTaskId() ? 'is-selected' : ''}"><td class="mono">${esc(task.task_id)}</td><td>${esc(taskLabel(task))}</td><td>${esc(task.current_gate || 'G0')}</td><td><button class="button button-danger delete-task" data-task-id="${esc(task.task_id)}" data-task-label="${esc(taskLabel(task))}">删除</button></td></tr>`).join('')}</tbody></table></div>
        </div>
        <div class="panel" id="import-panel"><div class="panel-title"><h3>导入访谈 TXT</h3><div class="actions"><input id="local-txt" type="file" accept=".txt,text/plain" hidden><button class="button button-primary" id="choose-txt">选择本地 TXT</button><button class="button" id="load-files">读取服务器目录</button><button class="button" id="rerun-task" ${!state.currentTask ? 'disabled' : ''}>从当前来源新建重跑任务</button></div></div><p class="muted">重复上传不会覆盖旧快照；需要从 G0 重跑时，请从当前不可变来源新建任务。</p><div id="file-list" class="empty">可选择本机新 TXT，或读取服务器输入目录中的文件</div></div>
      </div>
      <div>
                <div class="panel"><div class="panel-title"><h3>G1 目标专家与基线确认</h3>${badge(g1 ? '已通过' : '待确认', g1 ? 'green' : 'amber')}</div>
          <div class="form-grid">
            <label class="field field-full"><span>目标专家</span><input class="input" id="target-expert" value="${esc(state.currentTask?.target_expert || '')}" placeholder="例如：kiki chen"></label>
            <label class="field"><span>基线 Graph</span><select class="select" id="g1-baseline-select"><option value="">不使用既有基线</option>${state.baselines.map(b => `<option value="${esc(b.baseline_id)}" ${b.baseline_id === (state.currentTask?.baseline_id || '') ? 'selected' : ''}>${esc(b.name)} · v${esc(b.version)} · ${esc(short(b.content_hash, 10))}</option>`).join('')}</select></label>
            <label class="field"><span>责任人</span><input class="input" id="g1-reviewer" value="admin"></label>
            <label class="field"><span>原因</span><input class="input" id="g1-reason" value="确认本访谈的目标专家与基线"></label>
          </div>
          <div class="split-actions">
            <div>
              <input id="g1-drawio-file" type="file" accept=".drawio,.drawio.xml,.xml,.json" hidden>
              <button class="button" id="g1-import-drawio">导入基线 Graph（draw.io / JSON）</button>
              <input id="g1-script-file" type="file" accept=".txt,.docx,.doc,text/plain" hidden>
              <button class="button" id="g1-import-scripts">导入话术文档</button>
            </div>
            <button class="button button-primary" id="approve-g1" ${!state.currentTask ? 'disabled' : ''}>确认并通过 G1</button>
          </div>
          <p class="muted" style="margin-top:8px">G1 会精确绑定所选基线的 ID、版本与哈希。话术文档只生成待审映射候选，不会修改不可变基线。</p>
        </div>
        </div>
        <div class="panel"><div class="panel-title"><h3>完整访谈增量学习</h3>${badge(`${state.config.model || '--'} · 深度思考`, 'blue')}</div>
          <div class="actions"><button class="button button-primary" id="extract-strategy" ${!g1 ? 'disabled' : ''}>基于当前基线学习整份访谈</button><button class="button" id="map-scripts" ${!g1 ? 'disabled' : ''}>映射独立话术文档</button></div><p class="muted" style="margin-top:12px">模型读取完整基线和完整访谈，只输出有证据的增量变更。原始证据分类在后台完成，人工在 Graph 上核对结构、原话与时间位置。</p>
        </div>
        <div class="panel"><div class="panel-title"><h3>来源元数据</h3>${badge(source?.snapshot_redacted ? 'D3 已脱敏' : '无来源', source?.snapshot_redacted ? 'green' : '')}</div>${source ? `<dl class="detail-list"><div><dt>source_id</dt><dd class="mono">${esc(source.source_id)}</dd></div><div><dt>快照</dt><dd>${source.snapshot_available ? '已保存，不在接口返回正文' : '无'}</dd></div><div><dt>文件哈希</dt><dd class="mono">${esc(short(source.file_hash, 24))}</dd></div></dl>` : '<div class="empty">暂无来源</div>'}</div>
      </div>
    </div>${renderLog()}`;
}

function evidenceRows() {
  return state.evidence.map(e => {
    const reviewable = ['strategy', 'script', 'context', 'meta'].includes(e.evidence_kind);
    return `<tr data-id="${esc(e.evidence_id)}" class="${state.selectedEvidence?.evidence_id === e.evidence_id ? 'is-selected' : ''}"><td style="width:34px"><input class="checkbox evidence-check" type="checkbox" data-eid="${esc(e.evidence_id)}" ${state.selectedEvidenceIds.has(e.evidence_id) ? 'checked' : ''} ${reviewable ? '' : 'disabled title="请先归类"'}></td><td><span class="mono">${esc(e.timestamp)}</span><br><span class="muted">${esc(e.speaker)}</span></td><td>${esc(short(e.content, 84))}</td><td>${badge(e.evidence_kind || '未分类', reviewable ? 'blue' : 'red')}</td><td>${badge(e.status || 'candidate', e.status === 'approved' ? 'green' : e.conflict_set ? 'red' : 'amber')}</td></tr>`;
  }).join('');
}
function renderEvidence() {
  const e = state.selectedEvidence || state.evidence[0] || null;
  if (!state.selectedEvidence && e) state.selectedEvidence = e;
  const g1 = approvedGate('G1');
  const reviewableIds = state.evidence.filter(item => ['strategy','script','context','meta'].includes(item.evidence_kind)).map(item => item.evidence_id);
  const allSelected = reviewableIds.length > 0 && reviewableIds.every(id => state.selectedEvidenceIds.has(id));
  return `${viewHeading(`<button class="button" id="select-all">${allSelected ? '取消全选' : '全选'}</button><button class="button" id="select-pending">选择待审</button><button class="button" id="extract-all-strategy" ${!g1 ? 'disabled' : ''}>全部原文提炼 Graph（临时）</button><button class="button button-primary" id="approve-g2" ${!g1 ? 'disabled' : ''}>所选证据通过 G2</button>`)}
    <div class="layout">
      <div class="panel"><div class="panel-title"><h3>证据列表</h3>${badge(`${state.evidence.length} 条`, 'blue')}</div><div class="table-wrap"><table class="data-table"><thead><tr><th style="width:34px"></th><th style="width:120px">发言</th><th>内容</th><th style="width:95px">分类</th><th style="width:100px">状态</th></tr></thead><tbody>${evidenceRows()}</tbody></table></div></div>
      <div class="panel"><div class="panel-title"><h3>证据审核</h3>${e ? badge(e.evidence_id, 'blue') : ''}</div>${e ? `<p>${esc(e.content)}</p><dl class="detail-list"><div><dt>直接证据 ID</dt><dd class="mono">${esc(e.evidence_id)}</dd></div><div><dt>定位</dt><dd>${esc(e.utterance_id)} · ${esc(e.timestamp)}</dd></div><div><dt>冲突集</dt><dd>${esc(e.conflict_set || '无')}</dd></div></dl><div class="form-grid"><label class="field"><span>审核动作</span><select class="select" id="evidence-decision"><option value="accept">接受分类</option><option value="reclassify">改类</option><option value="pending">待确认</option><option value="conflict">保留冲突</option></select></label><label class="field"><span>证据分类</span><select class="select" id="evidence-kind">${['strategy','script','context','meta'].map(k => `<option ${e.evidence_kind === k ? 'selected' : ''}>${k}</option>`).join('')}</select></label><label class="field"><span>冲突集</span><input class="input" id="evidence-conflict" placeholder="冲突时必填" value="${esc(e.conflict_set || '')}"></label><label class="field"><span>责任人</span><input class="input" id="evidence-reviewer" value="admin"></label><label class="field field-full"><span>审核原因</span><input class="input" id="evidence-reason" value="人工复核访谈证据"></label></div><button class="button button-primary" id="review-evidence" style="margin-top:12px" ${!g1 ? 'disabled' : ''}>保存审核结果</button>` : '<div class="empty">暂无证据</div>'}</div>
    </div>${renderLog()}`;
}

function graphSummary() {
  const graph = state.selectedKnowledge?.type === 'graph' ? state.selectedKnowledge : state.knowledge.find(k => k.type === 'graph');
  if (!graph) return '<div class="empty">尚无 Graph。先在“任务与专家”中执行策略提炼。</div>';
  const link = graph.linkage || {};
  const byId = Object.fromEntries((state.knowledgeAll || state.knowledge).map(k => [k.object_id, k]));
  const relations = ids => (ids || []).map(id => byId[id]).filter(Boolean);
  const nodes = relations(link.node_ids), edges = relations(link.edge_ids), triggers = relations(link.trigger_ids);
  return `<div class="panel-title"><h3>${esc(graph.content)}</h3>${badge(graph.status, graph.status === 'approved' ? 'green' : 'amber')}</div><div class="graph-lanes"><div class="graph-lane"><h4>节点 · ${nodes.length}</h4>${nodes.map(n => `<div class="relation">${esc(n.content)}<small>${esc(n.object_id)}</small></div>`).join('') || '<span class="muted">无节点</span>'}</div><div class="graph-lane"><h4>边 · ${edges.length}</h4>${edges.map(edge => `<div class="relation">${esc(edge.linkage?.condition || edge.content)}<small>${esc(edge.linkage?.from_node_id)} → ${esc(edge.linkage?.to_node_id)}</small></div>`).join('') || '<span class="danger-note">无已持久化边</span>'}</div><div class="graph-lane"><h4>触发 / 停止 · ${triggers.length}</h4>${triggers.map(t => `<div class="relation">${esc(t.linkage?.condition || t.content)}<small>目标 ${esc(t.linkage?.target_node_id)}</small></div>`).join('') || '<span class="danger-note">无已持久化触发条件</span>'}${(link.stop_conditions || []).map(s => `<div class="relation">停止：${esc(s)}</div>`).join('')}</div></div>`;
}

function normalizeGraphLabel(value) {
  return String(value || '').trim().toLocaleLowerCase().replace(/[\s，。！？、；：,.!?;:]+/g, '');
}

function graphViewModel() {
  const all = state.knowledgeAll || state.knowledge;
  const byId = Object.fromEntries(all.map(item => [item.object_id, item]));
  const graphRecords = state.knowledge
    .filter(item => item.type === 'graph' && item.status !== 'archived')
    .sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')));
  const graph = graphRecords.find(item => item.object_id === state.graphCandidateId)
    || (state.selectedKnowledge?.type === 'graph' ? state.selectedKnowledge : null)
    || graphRecords.at(-1) || null;
  if (graph) state.graphCandidateId = graph.object_id;
  const linkage = graph?.linkage || {};
  const candidateNodes = (linkage.node_ids || []).map(id => byId[id]).filter(item => item?.type === 'strategy_node');
  const candidateEdges = (linkage.edge_ids || []).map(id => byId[id]).filter(item => item?.type === 'strategy_edge');
  const candidateTriggers = (linkage.trigger_ids || []).map(id => byId[id]).filter(item => item?.type === 'strategy_trigger');
  const candidateScripts = all.filter(item => item.type === 'script_fragment' && item.linkage?.node_id && item.linkage?.editor_variant !== true);
  const exactBaselineId = linkage.baseline_id || state.graphBaselineId;
  const baselineRecord = state.baselines.find(item => item.baseline_id === exactBaselineId) || null;
  const baseline = baselineRecord?.graph || null;
  const baselineNodeIds = new Set((baseline?.nodes || []).map(node => String(node.id)));
  const baselineNodeLabels = new Map((baseline?.nodes || []).map(node => [normalizeGraphLabel(node.label || node.name), String(node.id)]));
  const baselineEdgeIds = new Set((baseline?.edges || []).map(edge => String(edge.id)));
  const baselineEdgeLabels = new Map((baseline?.edges || []).map(edge => [normalizeGraphLabel(edge.label || edge.condition), String(edge.id)]));
  const resolveRef = (value, ids, labels) => {
    const raw = String(value || '').replace(/^base:/, '');
    return ids.has(raw) ? raw : (labels.get(normalizeGraphLabel(raw)) || null);
  };
  const baselineNodes = (baseline?.nodes || []).map(node => ({
    id: `base:${node.id}`, sourceId: String(node.id), label: node.label || node.name || node.id,
    kind: 'baseline', diffType: 'unchanged', status: node.status || 'reference', evidence_refs: node.evidence_refs || [],
    context_refs: [], script_evidence_refs: [], position: originalPosition(node), raw: node
  }));
  const baselineEdges = (baseline?.edges || []).map(edge => ({
    id: `base:${edge.id}`, source: `base:${edge.source || edge.from_node_id || edge.from}`,
    target: `base:${edge.target || edge.to_node_id || edge.to}`,
    rawCondition: edge.label || edge.condition || '',
    label: edge.condition_display || edge.label || edge.condition || '⚠ 分支条件缺失（无法判断走向）',
    conditionKind: edge.condition_kind || '',
    conditionReviewStatus: edge.condition_review_status || 'unreviewed',
    conditionUncertainty: edge.condition_uncertainty || '',
    sourceId: String(edge.id), kind: 'baseline', diffType: 'unchanged', status: edge.status || 'reference',
    evidence_refs: edge.evidence_refs || [], context_refs: [], raw: edge
  }));
  const candidateNodeItems = candidateNodes.map(node => {
    const nodeLink = node.linkage || {};
    const rawRefs = nodeLink.baseline_refs || (nodeLink.baseline_match ? [nodeLink.baseline_match] : []);
    const baselineRefs = rawRefs.map(ref => resolveRef(ref, baselineNodeIds, baselineNodeLabels)).filter(Boolean);
    return {
      id: `candidate:${node.object_id}`, candidateObjectId: node.object_id,
      candidateKey: nodeLink.candidate_key || node.object_id, label: node.content || node.object_id,
      kind: 'candidate', changeType: nodeLink.change_type || (baselineRefs.length ? 'modify' : 'add'),
      baselineRefs, changeReason: nodeLink.change_reason || '', status: node.status || 'candidate',
      evidence_refs: node.evidence_refs || [], context_refs: nodeLink.context_refs || [],
      script_evidence_refs: nodeLink.script_evidence_refs || [], position: nodeLink.position || null, raw: node
    };
  });
  const baselineRefCounts = new Map();
  candidateNodeItems.forEach(item => item.baselineRefs.forEach(ref => baselineRefCounts.set(ref, (baselineRefCounts.get(ref) || 0) + 1)));
  candidateNodeItems.forEach(item => {
    if (!item.raw?.linkage?.change_type && item.baselineRefs.some(ref => baselineRefCounts.get(ref) > 1)) item.changeType = 'split';
    item.possible_match_id = item.baselineRefs[0] ? `base:${item.baselineRefs[0]}` : null;
  });
  const candidateEdgeItems = candidateEdges.map(edge => ({
    id: `candidate:${edge.object_id}`, candidateObjectId: edge.object_id,
    candidateKey: edge.linkage?.candidate_key || edge.object_id,
    sourceRef: edge.linkage?.from_ref || edge.linkage?.from_node_id,
    targetRef: edge.linkage?.to_ref || edge.linkage?.to_node_id,
    rawCondition: edge.linkage?.condition || edge.content || '',
    label: edge.linkage?.condition || edge.content || '⚠ 分支条件缺失（无法判断走向）', kind: 'candidate',
    conditionReviewStatus: edge.linkage?.condition_review_status || (edge.linkage?.condition_uncertainty ? 'needs_review' : 'unreviewed'),
    conditionUncertainty: edge.linkage?.condition_uncertainty || '',
    changeType: edge.linkage?.change_type || ((edge.linkage?.baseline_refs || []).length ? 'modify' : 'add'),
    baselineRefs: (edge.linkage?.baseline_refs || []).map(ref => resolveRef(ref, baselineEdgeIds, baselineEdgeLabels)).filter(Boolean),
    changeReason: edge.linkage?.change_reason || '', status: edge.status || 'candidate',
    evidence_refs: edge.evidence_refs || [], context_refs: edge.linkage?.context_refs || [], raw: edge
  }));
  return {
    graph, graphRecords, baseline, candidateNodes: candidateNodeItems, candidateEdges: candidateEdgeItems,
    candidateTriggers, candidateScripts, baselineNodes, baselineEdges,
    baselineId: baselineRecord?.baseline_id || '',
    stopConditions: linkage.stop_conditions || baseline?.stop_conditions || []
  };
}

function originalPosition(node, offsetX = 0, offsetY = 0) {
  const position = node?.position;
  return position ? {
    x: Number(position.x || 0) + Number(position.width || 0) / 2 + offsetX,
    y: Number(position.y || 0) + Number(position.height || 0) / 2 + offsetY
  } : null;
}

function classifyDisplayedEdgeConditions(graph) {
  const semanticEdges = (graph.edges || []).filter(edge => edge.kind !== 'match');
  const outgoingCounts = new Map();
  semanticEdges.forEach(edge => outgoingCounts.set(edge.source, (outgoingCounts.get(edge.source) || 0) + 1));
  const issues = [];
  const edges = (graph.edges || []).map(edge => {
    if (edge.kind === 'match') return edge;
    const condition = String(edge.rawCondition ?? edge.raw?.label ?? edge.raw?.condition ?? '').trim();
    if (condition) {
      const classified = { ...edge, label: condition, rawCondition: condition, conditionKind: 'explicit' };
      if (edge.conditionUncertainty && edge.conditionReviewStatus !== 'confirmed') {
        classified.conditionKind = 'review_required_condition';
        issues.push({ ...classified, error: 'edge_condition_review_required' });
      }
      return classified;
    }
    if (outgoingCounts.get(edge.source) === 1) {
      return { ...edge, label: '完成上一步后继续', conditionKind: 'implicit_sequence' };
    }
    const classified = { ...edge, label: '⚠ 分支条件缺失', conditionKind: 'missing_branch_condition' };
    issues.push(classified);
    return classified;
  });
  const groups = new Map();
  edges.filter(edge => edge.kind !== 'match' && edge.rawCondition?.trim()).forEach(edge => {
    const key = `${edge.source}\u0000${normalizeGraphLabel(edge.rawCondition)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(edge);
  });
  groups.forEach(grouped => {
    if (new Set(grouped.map(edge => edge.target)).size <= 1) return;
    grouped.forEach(edge => { edge.conditionKind = 'conflicting_branch_condition'; });
    issues.push({ ...grouped[0], error: 'duplicate_branch_condition', edgeIds: grouped.map(edge => edge.id) });
  });
  return { ...graph, edges, conditionIssues: issues };
}

function renderConditionIssueNotice(issues, graph) {
  if (!issues.length) return '';
  const nodes = new Map((graph.nodes || []).map(node => [node.id, node]));
  const edges = new Map((graph.edges || []).map(edge => [edge.id, edge]));
  const nodeLabel = id => nodes.get(id)?.label || id || '未知节点';
  const rows = issues.map((issue, index) => {
    const edgeIds = issue.edgeIds?.length ? issue.edgeIds : [issue.id].filter(Boolean);
    const related = edgeIds.map(id => edges.get(id)).filter(Boolean);
    const first = related[0] || issue;
    const source = nodeLabel(first.source);
    let explanation;
    if (issue.error === 'duplicate_branch_condition') {
      const targets = related.map(edge => nodeLabel(edge.target)).join('、');
      explanation = `${source} 的条件“${first.rawCondition || first.label || ''}”同时指向 ${targets}。仅确认不会解除冲突，需将各条条件改成可区分的语义。`;
    } else if (issue.error === 'edge_condition_review_required') {
      explanation = `${source} → ${nodeLabel(first.target)}：模型标记条件存在不确定性，需要保存并确认。`;
    } else {
      explanation = `${source} → ${nodeLabel(first.target)}：多出口边缺少路由条件。`;
    }
    const buttons = related.map(edge => `<button class="button graph-condition-issue-edge" data-condition-edge-id="${esc(edge.id)}">查看 → ${esc(nodeLabel(edge.target))}</button>`).join('');
    return `<div class="graph-condition-issue"><b>问题 ${index + 1}</b><span>${esc(explanation)}</span><div class="actions">${buttons}</div></div>`;
  }).join('');
  return `<div class="notice notice-red"><b>有 ${issues.length} 项路由条件问题，已阻断整图批准与 Prompt 编译。</b><div class="graph-condition-issues">${rows}</div></div>`;
}

function materializedCandidate(model) {
  const changedBaselineNodes = new Set(model.candidateNodes.flatMap(node => node.baselineRefs));
  const changedBaselineEdges = new Set(model.candidateEdges.flatMap(edge => edge.baselineRefs));
  const endpointMap = new Map();
  const nodes = [];
  model.baselineNodes.forEach(node => {
    if (changedBaselineNodes.has(node.sourceId)) return;
    const resultId = `result:${node.sourceId}`;
    endpointMap.set(node.sourceId, resultId);
    endpointMap.set(node.id, resultId);
    nodes.push({ ...node, id: resultId, kind: 'inherited', diffType: 'unchanged', position: originalPosition(node.raw) });
  });
  model.candidateNodes.forEach(node => {
    if (node.changeType === 'deprecate') return;
    const resultId = `result:${node.candidateObjectId}`;
    const anchor = model.baselineNodes.find(base => base.sourceId === node.baselineRefs[0]);
    nodes.push({ ...node, id: resultId, kind: 'candidate', diffType: node.changeType, position: originalPosition(anchor?.raw) || node.position });
    endpointMap.set(node.candidateObjectId, resultId);
    endpointMap.set(node.candidateKey, resultId);
    node.baselineRefs.forEach(ref => { if (!endpointMap.has(ref)) endpointMap.set(ref, resultId); });
  });
  const edges = [];
  model.baselineEdges.forEach(edge => {
    if (changedBaselineEdges.has(edge.sourceId)) return;
    const rawSource = String(edge.raw.source || edge.raw.from_node_id || edge.raw.from || '');
    const rawTarget = String(edge.raw.target || edge.raw.to_node_id || edge.raw.to || '');
    const source = endpointMap.get(rawSource);
    const target = endpointMap.get(rawTarget);
    if (source && target) edges.push({ ...edge, id: `result:${edge.sourceId}`, source, target, kind: 'inherited', diffType: 'unchanged' });
  });
  model.candidateEdges.forEach(edge => {
    if (edge.changeType === 'deprecate') return;
    const sourceRef = String(edge.sourceRef || '').replace(/^base:/, '');
    const targetRef = String(edge.targetRef || '').replace(/^base:/, '');
    const source = endpointMap.get(sourceRef) || endpointMap.get(edge.sourceRef);
    const target = endpointMap.get(targetRef) || endpointMap.get(edge.targetRef);
    if (source && target) edges.push({ ...edge, id: `result:${edge.candidateObjectId}`, source, target, diffType: edge.changeType });
  });
  return classifyDisplayedEdgeConditions({ nodes, edges });
}

function diffGraph(model) {
  const nodeChanges = new Map();
  model.candidateNodes.forEach(node => node.baselineRefs.forEach(ref => {
    if (!nodeChanges.has(ref)) nodeChanges.set(ref, []);
    nodeChanges.get(ref).push(node);
  }));
  const edgeChanges = new Map();
  model.candidateEdges.forEach(edge => edge.baselineRefs.forEach(ref => {
    if (!edgeChanges.has(ref)) edgeChanges.set(ref, []);
    edgeChanges.get(ref).push(edge);
  }));
  const nodes = model.baselineNodes.map(node => ({
    ...node, id: `old:${node.sourceId}`, kind: 'baseline',
    diffType: nodeChanges.get(node.sourceId)?.some(change => change.changeType === 'deprecate') ? 'removed'
      : nodeChanges.has(node.sourceId) ? 'changed-old' : 'unchanged',
    position: originalPosition(node.raw)
  }));
  const endpointMap = new Map(model.baselineNodes.map(node => [node.sourceId, `old:${node.sourceId}`]));
  model.candidateNodes.forEach(node => {
    if (node.changeType === 'deprecate') return;
    const id = `new:${node.candidateObjectId}`;
    const anchor = model.baselineNodes.find(base => base.sourceId === node.baselineRefs[0]);
    nodes.push({ ...node, id, kind: 'candidate', diffType: node.changeType, position: originalPosition(anchor?.raw, 190, 0) || node.position });
    endpointMap.set(node.candidateObjectId, id);
    endpointMap.set(node.candidateKey, id);
    node.baselineRefs.forEach(ref => endpointMap.set(ref, id));
  });
  const edges = model.baselineEdges.map(edge => ({
    ...edge, id: `old:${edge.sourceId}`,
    source: `old:${String(edge.raw.source || edge.raw.from_node_id || edge.raw.from || '')}`,
    target: `old:${String(edge.raw.target || edge.raw.to_node_id || edge.raw.to || '')}`,
    kind: 'baseline', diffType: edgeChanges.get(edge.sourceId)?.some(change => change.changeType === 'deprecate') ? 'removed'
      : edgeChanges.has(edge.sourceId) ? 'changed-old' : 'unchanged'
  }));
  model.candidateEdges.forEach(edge => {
    if (edge.changeType === 'deprecate') return;
    const sourceRef = String(edge.sourceRef || '').replace(/^base:/, '');
    const targetRef = String(edge.targetRef || '').replace(/^base:/, '');
    const source = endpointMap.get(sourceRef) || endpointMap.get(edge.sourceRef);
    const target = endpointMap.get(targetRef) || endpointMap.get(edge.targetRef);
    if (source && target) edges.push({ ...edge, id: `new:${edge.candidateObjectId}`, source, target, diffType: edge.changeType });
  });
  model.candidateNodes.forEach(node => node.baselineRefs.forEach(ref => {
    if (node.changeType !== 'deprecate') edges.push({ id: `match:${ref}:${node.candidateObjectId}`, source: `old:${ref}`, target: `new:${node.candidateObjectId}`, label: node.changeType, kind: 'match', diffType: 'match', raw: node.raw });
  }));
  return classifyDisplayedEdgeConditions({ nodes, edges });
}

function graphElements(model) {
  if (state.graphMode === 'candidate') return materializedCandidate(model);
  if (state.graphMode === 'diff') return diffGraph(model);
  return classifyDisplayedEdgeConditions({ nodes: model.baselineNodes, edges: model.baselineEdges });
}

const CALL_FLOW_PHASES = [
  { phase_id: 'pre_call', label: '外呼前准备', order: 1 },
  { phase_id: 'connect_permission', label: '接通与身份许可', order: 2 },
  { phase_id: 'availability_routing', label: '可用性与初始分流', order: 3 },
  { phase_id: 'intent_objection', label: '意愿识别与异议处理', order: 4 },
  { phase_id: 'needs_matching', label: '需求澄清与机会匹配', order: 5 },
  { phase_id: 'conversion', label: '转化动作', order: 6 },
  { phase_id: 'closure_followup', label: '收口与后续', order: 7 },
];
const CALL_FLOW_BRANCH_OFFSET = { resistant: -300, neutral: 0, receptive: 300, unknown: 0 };

function graphItemLayoutId(item) {
  return String(item?.candidateObjectId || item?.sourceId || '').trim();
}

function stableLayoutUnit(value) {
  let hash = 2166136261;
  for (const character of String(value || '')) {
    hash ^= character.charCodeAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) / 4294967295;
}

function callFlowLayout(nodes, edges, profile) {
  const phases = [...(profile?.phases || CALL_FLOW_PHASES), { phase_id: 'unassigned', label: '待分类', order: 8 }];
  const phaseOrder = new Map(phases.map(phase => [phase.phase_id, Number(phase.order)]));
  const phaseFor = node => {
    const phase = profile?.node_annotations?.[graphItemLayoutId(node)]?.phase_id || 'unassigned';
    return phaseOrder.has(phase) ? phase : 'unassigned';
  };
  const nodePhaseOrders = new Map(nodes.map(node => [node.id, phaseOrder.get(phaseFor(node)) || 8]));
  const strategyEdges = edges.filter(edge => edge.kind !== 'match' && nodePhaseOrders.has(edge.source) && nodePhaseOrders.has(edge.target));
  const tendencyFor = edge => {
    const value = profile?.edge_annotations?.[graphItemLayoutId(edge)]?.route_tendency || 'unknown';
    return Object.hasOwn(CALL_FLOW_BRANCH_OFFSET, value) ? value : 'unknown';
  };
  const incoming = new Map(nodes.map(node => [node.id, []]));
  const outgoing = new Map(nodes.map(node => [node.id, []]));
  strategyEdges.forEach(edge => {
    incoming.get(edge.target)?.push(edge);
    outgoing.get(edge.source)?.push(edge);
  });

  // 分支方向是“相对来源节点”的展示倾向，不再把整图切成固定的左/中/右三栏。
  const edgeIdealDx = new Map();
  const edgeFanRank = new Map();
  outgoing.forEach(sourceEdges => {
    const groups = new Map();
    sourceEdges.forEach(edge => {
      const tendency = tendencyFor(edge);
      if (!groups.has(tendency)) groups.set(tendency, []);
      groups.get(tendency).push(edge);
    });
    groups.forEach((group, tendency) => {
      group.sort((a, b) => String(a.id).localeCompare(String(b.id)));
      group.forEach((edge, index) => {
        const fanRank = index - (group.length - 1) / 2;
        edgeFanRank.set(edge.id, fanRank);
        edgeIdealDx.set(edge.id, CALL_FLOW_BRANCH_OFFSET[tendency] + fanRank * 150);
      });
    });
  });

  const nodesByPhase = new Map(phases.map(phase => [phase.phase_id, []]));
  nodes.forEach(node => nodesByPhase.get(phaseFor(node)).push(node));
  const phaseMetrics = new Map();
  let top = 50;
  phases.forEach(phase => {
    const count = nodesByPhase.get(phase.phase_id).length;
    const rows = Math.max(1, Math.min(5, Math.ceil(Math.sqrt(Math.max(1, count)))));
    const height = Math.max(270, 160 + rows * 115);
    phaseMetrics.set(phase.phase_id, { top, height, center: top + height / 2, rows });
    top += height + 105;
  });

  const positions = new Map();
  const initialX = new Map();
  const graphCenter = 1500;
  phases.forEach(phase => {
    const group = nodesByPhase.get(phase.phase_id).sort((a, b) => graphItemLayoutId(a).localeCompare(graphItemLayoutId(b)));
    const metric = phaseMetrics.get(phase.phase_id);
    group.forEach((node, index) => {
      const parentTargets = (incoming.get(node.id) || []).map(edge => {
        const parent = positions.get(edge.source);
        return parent ? parent.x + (edgeIdealDx.get(edge.id) || 0) : null;
      }).filter(Number.isFinite);
      const rootOffset = (index - (group.length - 1) / 2) * 250;
      const x = (parentTargets.length ? parentTargets.reduce((sum, value) => sum + value, 0) / parentTargets.length : graphCenter + rootOffset)
        + (stableLayoutUnit(node.id) - 0.5) * 50;
      const row = index % metric.rows;
      const y = metric.center + (row - (metric.rows - 1) / 2) * 102 + (stableLayoutUnit(`${node.id}:y`) - 0.5) * 12;
      positions.set(node.id, { x, y });
      initialX.set(node.id, x);
    });
  });

  // 确定性的轻量约束松弛：阶段吸附、亲子边弹簧、节点排斥。
  const sortedNodes = [...nodes].sort((a, b) => String(a.id).localeCompare(String(b.id)));
  for (let iteration = 0; iteration < 180; iteration += 1) {
    const forces = new Map(sortedNodes.map(node => [node.id, { x: 0, y: 0 }]));
    sortedNodes.forEach(node => {
      const point = positions.get(node.id);
      const metric = phaseMetrics.get(phaseFor(node));
      forces.get(node.id).y += (metric.center - point.y) * 0.035;
      forces.get(node.id).x += (initialX.get(node.id) - point.x) * 0.006;
    });
    strategyEdges.forEach(edge => {
      const source = positions.get(edge.source);
      const target = positions.get(edge.target);
      const sourceForce = forces.get(edge.source);
      const targetForce = forces.get(edge.target);
      if (!source || !target || !sourceForce || !targetForce) return;
      const backEdge = (nodePhaseOrders.get(edge.source) || 0) > (nodePhaseOrders.get(edge.target) || 99);
      const desiredDx = edgeIdealDx.get(edge.id) || 0;
      const xError = (target.x - source.x) - desiredDx;
      const spring = backEdge ? 0.006 : 0.018;
      sourceForce.x += xError * spring;
      targetForce.x -= xError * spring;
      if (nodePhaseOrders.get(edge.source) === nodePhaseOrders.get(edge.target)) {
        const desiredDy = edge.source === edge.target ? 0 : 115;
        const yError = (target.y - source.y) - desiredDy;
        sourceForce.y += yError * 0.009;
        targetForce.y -= yError * 0.009;
      }
    });
    for (let leftIndex = 0; leftIndex < sortedNodes.length; leftIndex += 1) {
      for (let rightIndex = leftIndex + 1; rightIndex < sortedNodes.length; rightIndex += 1) {
        const left = sortedNodes[leftIndex];
        const right = sortedNodes[rightIndex];
        if (nodePhaseOrders.get(left.id) !== nodePhaseOrders.get(right.id)) continue;
        const a = positions.get(left.id);
        const b = positions.get(right.id);
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        if (Math.abs(dx) >= 230 || Math.abs(dy) >= 94) continue;
        if (Math.abs(dx) < 1) dx = stableLayoutUnit(`${left.id}:${right.id}`) < 0.5 ? -1 : 1;
        if (Math.abs(dy) < 1) dy = stableLayoutUnit(`${right.id}:${left.id}`) < 0.5 ? -1 : 1;
        const horizontalOverlap = 230 - Math.abs(dx);
        const verticalOverlap = 94 - Math.abs(dy);
        if (horizontalOverlap / 230 >= verticalOverlap / 94) {
          const push = Math.sign(dy) * Math.min(12, verticalOverlap * 0.14);
          forces.get(left.id).y -= push;
          forces.get(right.id).y += push;
        } else {
          const push = Math.sign(dx) * Math.min(18, horizontalOverlap * 0.16);
          forces.get(left.id).x -= push;
          forces.get(right.id).x += push;
        }
      }
    }
    sortedNodes.forEach(node => {
      const point = positions.get(node.id);
      const force = forces.get(node.id);
      const metric = phaseMetrics.get(phaseFor(node));
      point.x += Math.max(-24, Math.min(24, force.x));
      point.y = Math.max(metric.top + 70, Math.min(metric.top + metric.height - 55, point.y + Math.max(-14, Math.min(14, force.y))));
    });
  }

  const minimumX = Math.min(...nodes.map(node => positions.get(node.id)?.x).filter(Number.isFinite));
  const shiftX = 190 - minimumX;
  positions.forEach(point => { point.x = Math.round(point.x + shiftX); point.y = Math.round(point.y); });
  nodes.forEach(node => {
    const saved = profile?.manual_positions?.[graphItemLayoutId(node)];
    if (Number.isFinite(Number(saved?.x)) && Number.isFinite(Number(saved?.y))) {
      positions.set(node.id, { x: Number(saved.x), y: Number(saved.y) });
    }
  });
  const finalX = nodes.map(node => positions.get(node.id)?.x).filter(Number.isFinite);
  const finalMinimumX = Math.min(...finalX);
  const finalMaximumX = Math.max(...finalX);
  const width = Math.max(1400, finalMaximumX - finalMinimumX + 380);
  const guideCenterX = (finalMinimumX + finalMaximumX) / 2;
  const guides = phases.map(phase => {
    const metric = phaseMetrics.get(phase.phase_id);
    return {
      data: { id: `layout:phase:${phase.phase_id}`, label: `${phase.order <= 7 ? `${phase.order}. ` : ''}${phase.label}`, guide: 'phase', guideWidth: width - 60, guideHeight: metric.height },
      position: { x: guideCenterX, y: metric.center }, selectable: false, grabbable: false,
    };
  });
  const backEdgeIds = new Set(edges.filter(edge =>
    (nodePhaseOrders.get(edge.source) || 0) > (nodePhaseOrders.get(edge.target) || 99)
  ).map(edge => edge.id));
  const edgeRoutes = new Map();
  strategyEdges.forEach(edge => {
    const rank = edgeFanRank.get(edge.id) || 0;
    const naturalSign = (edgeIdealDx.get(edge.id) || 0) < 0 ? -1 : 1;
    const baseDistance = 24 + Math.abs(rank) * 46;
    edgeRoutes.set(edge.id, {
      curveDistance: Math.round((backEdgeIds.has(edge.id) ? 145 + baseDistance : baseDistance) * (rank ? Math.sign(rank) : naturalSign)),
      curveWeight: backEdgeIds.has(edge.id) ? 0.42 : 0.5,
    });
  });
  const firstPhaseOrder = Math.min(...nodes.map(node => nodePhaseOrders.get(node.id)).filter(Number.isFinite));
  const firstPhaseX = nodes.filter(node => nodePhaseOrders.get(node.id) === firstPhaseOrder).map(node => positions.get(node.id)?.x).filter(Number.isFinite);
  const focusX = firstPhaseX.length ? firstPhaseX.reduce((sum, value) => sum + value, 0) / firstPhaseX.length : width / 2;
  return { positions, guides, backEdgeIds, edgeRoutes, width, height: top, focusX };
}

function renderNodeLayoutEditor(item, model) {
  const profile = state.graphLayoutProfile;
  const itemId = graphItemLayoutId(item);
  const annotation = profile?.node_annotations?.[itemId] || { phase_id: 'unassigned', source: 'unassigned' };
  const editable = Boolean(profile && ['ready', 'partial'].includes(profile.status));
  const phases = profile?.phases || CALL_FLOW_PHASES;
  return `<section class="graph-layout-editor" data-layout-kind="node" data-item-id="${esc(itemId)}">
    <div class="panel-title"><h4>电话流程布局标注</h4>${badge(annotation.source || 'unassigned', annotation.source === 'manual' ? 'green' : 'blue')}</div>
    <p class="muted">只影响阶段位置；横向位置会根据各条分支相对母节点自动展开，不修改策略节点，也不进入 Prompt。</p>
    <label class="field"><span>电话流程阶段</span><select class="select layout-phase" ${editable ? '' : 'disabled'}><option value="" ${annotation.phase_id === 'unassigned' ? 'selected' : ''}>待分类</option>${phases.map(phase => `<option value="${esc(phase.phase_id)}" ${annotation.phase_id === phase.phase_id ? 'selected' : ''}>${phase.order}. ${esc(phase.label)}</option>`).join('')}</select></label>
    <div class="actions"><button class="button button-primary save-layout-annotation" ${editable ? '' : 'disabled'}>保存布局标注</button><button class="button reset-layout-annotation" ${editable && annotation.source === 'manual' ? '' : 'disabled'}>恢复自动判断</button></div>
  </section>`;
}

function renderEdgeLayoutEditor(item) {
  const profile = state.graphLayoutProfile;
  const itemId = graphItemLayoutId(item);
  const annotation = profile?.edge_annotations?.[itemId] || { route_tendency: 'unknown', source: 'unassigned' };
  const editable = Boolean(profile && ['ready', 'partial'].includes(profile.status));
  const options = [['resistant', '相对来源节点向左 · 抗拒 / 负向'], ['neutral', '相对来源节点向下 · 中性'], ['receptive', '相对来源节点向右 · 接受 / 正向'], ['unknown', '接近来源节点正下方 · 未知 / 不判断']];
  return `<section class="graph-layout-editor" data-layout-kind="edge" data-item-id="${esc(itemId)}">
    <div class="panel-title"><h4>分支展示方向</h4>${badge(annotation.source || 'unassigned', annotation.source === 'manual' ? 'green' : 'blue')}</div>
    <p class="muted">只决定目标节点相对本条边来源节点的偏移方向；边上的路由条件仍是唯一权威语义。</p>
    <label class="field"><span>相对分支方向</span><select class="select layout-tendency" ${editable ? '' : 'disabled'}>${options.map(([value, label]) => `<option value="${value}" ${annotation.route_tendency === value ? 'selected' : ''}>${label}</option>`).join('')}</select></label>
    <div class="actions"><button class="button button-primary save-layout-annotation" ${editable ? '' : 'disabled'}>保存布局标注</button><button class="button reset-layout-annotation" ${editable && annotation.source === 'manual' ? '' : 'disabled'}>恢复自动判断</button></div>
  </section>`;
}

function bindLayoutEditor(item, model) {
  const host = document.querySelector('.graph-layout-editor');
  if (!host || !state.graphLayoutProfile) return;
  const controls = [...host.querySelectorAll('select')];
  controls.forEach(control => control.onchange = () => { host.dataset.dirty = '1'; });
  const save = async clearManual => {
    const common = {
      task_id: currentTaskId(), graph_id: model.graph.object_id,
      materialized_graph_hash: state.graphLayoutProfile.materialized_graph_hash,
      editor: document.getElementById('graph-reviewer')?.value.trim() || 'admin',
    };
    if (host.dataset.layoutKind === 'node') {
      const phaseId = host.querySelector('.layout-phase').value;
      if (!clearManual && !phaseId) return log('请先选择电话流程阶段', 'error');
      common.node_updates = [{ node_id: host.dataset.itemId, phase_id: phaseId, lane_override: null, clear_manual: clearManual }];
    } else {
      common.edge_updates = [{ edge_id: host.dataset.itemId, route_tendency: host.querySelector('.layout-tendency').value, clear_manual: clearManual }];
    }
    const result = await post('/graph-layout', common);
    if (result.error) return log(`布局标注保存失败：${errorText(result)}`, 'error');
    state.graphLayoutProfile = result;
    renderGraphCanvas();
    const refreshedModel = graphViewModel();
    const displayed = graphElements(refreshedModel);
    const refreshed = [...displayed.nodes, ...displayed.edges].find(entry => graphItemLayoutId(entry) === host.dataset.itemId && Boolean(entry.source) === (host.dataset.layoutKind === 'edge'));
    if (refreshed) graphDetail(refreshed, refreshedModel);
    log(clearManual ? '已恢复系统自动布局判断' : '布局标注已保存；策略 Graph 与 Prompt 未改变', 'ok');
  };
  const saveButton = host.querySelector('.save-layout-annotation');
  const resetButton = host.querySelector('.reset-layout-annotation');
  if (saveButton) saveButton.onclick = event => runButton(event.currentTarget, '保存中...', () => save(false));
  if (resetButton) resetButton.onclick = event => runButton(event.currentTarget, '恢复中...', () => save(true));
}

function evidenceForRefs(refs) {
  const wanted = new Set(refs || []);
  return state.evidence.filter(item => wanted.has(item.evidence_id) || wanted.has(item.utterance_id));
}

function renderEvidenceCards(items, tone = '') {
  return items.map(item => `<article class="evidence-card ${tone ? `evidence-card-${tone}` : ''}"><header><strong>${esc(item.speaker || '未知说话人')}</strong><time>${esc(item.timestamp || '无时间位置')}</time></header><p>${esc(item.content)}</p><footer><span class="mono">${esc(item.evidence_id)}</span><span>回听定位：${esc(item.timestamp || '未提供')}</span></footer></article>`).join('');
}

function scriptVersion(workspaceItem, variantId) {
  return (workspaceItem.versions || []).find(version => version.variant_id === variantId) || null;
}

function renderScriptWorkspace(workspace, preferredVariantId = '') {
  const editable = workspace.editable;
  const cards = (workspace.items || []).map(item => {
    const activeId = preferredVariantId && item.versions.some(version => version.variant_id === preferredVariantId)
      ? preferredVariantId : (item.selected_variant_id || '');
    const active = scriptVersion(item, activeId);
    const text = active?.content ?? item.source_text;
    const options = [`<option value="" ${!activeId ? 'selected' : ''}>访谈初始版</option>`, ...(item.versions || []).map(version =>
      `<option value="${esc(version.variant_id)}" ${version.variant_id === activeId ? 'selected' : ''}>保存版本 · ${esc(String(version.created_at || '').replace('T', ' '))} · ${esc(version.status)}</option>`
    )].join('');
    const disabled = editable ? '' : 'disabled';
    const deleteDisabled = !editable || !activeId || active?.status === 'approved' ? 'disabled' : '';
    return `<article class="script-version-card" data-evidence-id="${esc(item.evidence_id)}">
      <header><label class="script-use"><input type="checkbox" class="script-use-check" ${item.selected ? 'checked' : ''} ${disabled}> 用于节点话术</label><time>${esc(item.timestamp || '无时间位置')}</time></header>
      <div class="script-source-meta"><strong>${esc(item.speaker || '未知说话人')}</strong><span class="mono">${esc(item.evidence_id)}</span></div>
      <label class="field"><span>采用版本</span><select class="select script-version-select" data-previous-value="${esc(activeId)}" ${disabled}>${options}</select></label>
      <textarea class="input script-editor" rows="8" data-loaded-text="${esc(text)}" ${disabled}>${esc(text)}</textarea>
      <p class="script-dirty-note" hidden>有尚未保存的新修改</p>
      <div class="actions script-version-actions"><button class="button save-script-variant" ${disabled}>保存为新版本</button><button class="button reset-script-editor" ${disabled}>重置为访谈初始版</button><button class="button button-danger delete-script-variant" ${deleteDisabled}>永久删除此版本</button></div>
      <footer><span>初始证据永不修改</span><span>回听定位：${esc(item.timestamp || '未提供')}</span></footer>
    </article>`;
  }).join('');
  return `<div class="script-workspace-summary"><strong>共 ${workspace.items.length} 条 · 已选择 ${workspace.selected_count} 条</strong><span>${editable ? '保存版本后，再保存本节点选择；最终随整图批准生效。' : '已批准 Graph 只读。'}</span></div>
    ${cards || '<div class="empty">当前节点没有目标专家原话</div>'}
    ${workspace.items.length ? `<button class="button button-primary save-script-selections" ${editable ? '' : 'disabled'}>保存本节点话术选择</button>` : ''}`;
}

function setScriptEditorText(card, text) {
  const editor = card.querySelector('.script-editor');
  editor.value = text;
  editor.dataset.loadedText = text;
  editor.dataset.dirty = '0';
  editor.classList.remove('is-dirty');
  card.querySelector('.script-dirty-note').hidden = true;
  syncGraphApprovalDirtyState();
}

function syncGraphApprovalDirtyState() {
  const approve = document.getElementById('approve-graph');
  if (!approve) return;
  if (approve.dataset.baseDisabled == null) approve.dataset.baseDisabled = approve.disabled ? '1' : '0';
  const dirty = Boolean(document.querySelector('#graph-detail [data-dirty="1"]'));
  approve.disabled = approve.dataset.baseDisabled === '1' || dirty;
  approve.title = dirty ? '请先保存或重置右栏未保存修改' : '';
}

function updateScriptEditorDirty(card) {
  const editor = card.querySelector('.script-editor');
  const dirty = editor.value !== editor.dataset.loadedText;
  editor.dataset.dirty = dirty ? '1' : '0';
  editor.classList.toggle('is-dirty', dirty);
  card.querySelector('.script-dirty-note').hidden = !dirty;
  syncGraphApprovalDirtyState();
}

function bindScriptWorkspace(workspace, item, model) {
  const host = document.getElementById('script-workspace');
  if (!host || host.dataset.nodeId !== workspace.node_id) return;
  host.querySelectorAll('.script-version-card').forEach(card => {
    const source = workspace.items.find(entry => entry.evidence_id === card.dataset.evidenceId);
    const select = card.querySelector('.script-version-select');
    const editor = card.querySelector('.script-editor');
    const deleteButton = card.querySelector('.delete-script-variant');
    editor.oninput = () => updateScriptEditorDirty(card);
    select.onchange = () => {
      if (editor.dataset.dirty === '1' && !window.confirm('当前原话有未保存修改，是否放弃并切换版本？')) {
        select.value = select.dataset.previousValue || '';
        return;
      }
      const version = scriptVersion(source, select.value);
      setScriptEditorText(card, version?.content ?? source.source_text);
      select.dataset.previousValue = select.value;
      deleteButton.disabled = !workspace.editable || !select.value || version?.status === 'approved';
    };
    card.querySelector('.reset-script-editor').onclick = () => {
      editor.value = source.source_text;
      updateScriptEditorDirty(card);
    };
    card.querySelector('.save-script-variant').onclick = event => runButton(event.currentTarget, '保存中...', async () => {
      const result = await post('/script-variants', {
        task_id: currentTaskId(), graph_id: model.graph.object_id, node_id: item.candidateObjectId,
        evidence_id: source.evidence_id, content: editor.value, editor: document.getElementById('graph-reviewer')?.value.trim() || 'admin'
      });
      if (result.error) return log(`原话版本保存失败：${errorText(result)}`, 'error');
      await loadScriptWorkspace(item, model, result.variant.object_id);
      log(result.deduplicated ? '相同原话版本已存在，已切换到该版本' : '人工校准原话已保存为新版本', 'ok');
    });
    deleteButton.onclick = event => {
      const versionId = select.value;
      if (!versionId || !window.confirm('永久删除这个未批准版本？如果它正被采用，该条会自动回退到访谈初始版。')) return;
      runButton(event.currentTarget, '删除中...', async () => {
        const result = await post('/script-variants/delete', { task_id: currentTaskId(), variant_id: versionId, editor: document.getElementById('graph-reviewer')?.value.trim() || 'admin' });
        if (result.error) return log(`原话版本删除失败：${errorText(result)}`, 'error');
        await loadScriptWorkspace(item, model);
        log('未批准原话版本已永久删除', 'ok');
      });
    };
  });
  const saveSelections = host.querySelector('.save-script-selections');
  if (saveSelections) saveSelections.onclick = event => runButton(event.currentTarget, '保存选择中...', async () => {
    const dirty = host.querySelector('.script-editor[data-dirty="1"]');
    if (dirty) { dirty.focus(); return log('仍有未保存原话，请先保存为版本或重置后再保存节点选择', 'error'); }
    const selections = [...host.querySelectorAll('.script-version-card')].filter(card => card.querySelector('.script-use-check').checked).map(card => ({
      evidence_id: card.dataset.evidenceId, variant_id: card.querySelector('.script-version-select').value || null
    }));
    const result = await post('/script-selections', { task_id: currentTaskId(), graph_id: model.graph.object_id, node_id: item.candidateObjectId, selections, editor: document.getElementById('graph-reviewer')?.value.trim() || 'admin' });
    if (result.error) return log(`节点话术选择保存失败：${errorText(result)}`, 'error');
    state.scriptWorkspaces.set(`${model.graph.object_id}:${item.candidateObjectId}`, result);
    host.innerHTML = renderScriptWorkspace(result);
    bindScriptWorkspace(result, item, model);
    log(`本节点已保存 ${result.selected_count} 条话术范例`, 'ok');
  });
}

async function loadScriptWorkspace(item, model, preferredVariantId = '') {
  const host = document.getElementById('script-workspace');
  if (!host || !model.graph || !item.candidateObjectId) return;
  const nodeId = item.candidateObjectId;
  host.dataset.nodeId = nodeId;
  const query = new URLSearchParams({ task_id: currentTaskId(), graph_id: model.graph.object_id, node_id: nodeId });
  const result = await api(`/script-variants?${query}`);
  if (!document.getElementById('script-workspace') || host.dataset.nodeId !== nodeId) return;
  if (result.error) { host.innerHTML = `<p class="danger-note">原话版本加载失败：${esc(errorText(result))}</p>`; return; }
  state.scriptWorkspaces.set(`${model.graph.object_id}:${nodeId}`, result);
  host.innerHTML = renderScriptWorkspace(result, preferredVariantId);
  bindScriptWorkspace(result, item, model);
}

function renderNodeContentWorkspace(workspace) {
  const disabled = workspace.editable ? '' : 'disabled';
  return `<section class="edge-condition-workspace node-content-workspace">
    <div class="panel-title"><h4>节点内容人工校准</h4>${badge(workspace.content_review_status || 'unreviewed', workspace.content_review_status === 'confirmed' ? 'green' : 'amber')}</div>
    <p class="muted">导入基线保持不可变；保存既有节点会生成候选 modify。缺少证据或话术时保持为空，不自动补写。</p>
    <label class="field"><span>当前节点名称 / 策略说明</span><textarea class="textarea node-content-editor" data-saved="${esc(workspace.current_content || '')}" ${disabled}>${esc(workspace.current_content || '')}</textarea></label>
    <p class="node-dirty-note" hidden>有未保存修改</p>
    <div class="actions"><button class="button button-primary save-node-content" ${disabled}>保存节点修改</button><button class="button reset-node-content" ${disabled}>重置为原始内容</button></div>
    <dl class="detail-list"><div><dt>原始提炼 / 导入内容</dt><dd>${esc(workspace.original_content || '原始内容为空')}</dd></div><div><dt>已有字段</dt><dd>证据 ${workspace.evidence_refs?.length || 0} · 上下文 ${workspace.context_refs?.length || 0} · 话术 ${(workspace.scripts?.length || 0) + (workspace.expert_utterances?.length || 0)}</dd></div></dl>
  </section>`;
}

function updateNodeEditorDirty(host) {
  const editor = host.querySelector('.node-content-editor');
  if (!editor) return;
  const dirty = editor.value !== editor.dataset.saved;
  editor.dataset.dirty = dirty ? '1' : '0';
  editor.classList.toggle('is-dirty', dirty);
  const note = host.querySelector('.node-dirty-note');
  if (note) note.hidden = !dirty;
  syncGraphApprovalDirtyState();
}

function bindNodeContentWorkspace(workspace, item, model) {
  const host = document.getElementById('node-content-workspace');
  if (!host) return;
  const editor = host.querySelector('.node-content-editor');
  if (!editor || !workspace.editable) return;
  editor.oninput = () => updateNodeEditorDirty(host);
  host.querySelector('.reset-node-content').onclick = () => {
    editor.value = workspace.original_content || '';
    updateNodeEditorDirty(host);
  };
  host.querySelector('.save-node-content').onclick = event => runButton(event.currentTarget, '保存中...', async () => {
    const content = editor.value.trim();
    if (!content) return log('节点名称 / 策略说明不能为空', 'error');
    const result = await post('/node-content', {
      task_id: currentTaskId(), graph_id: model.graph.object_id,
      node_origin: workspace.node_origin, node_id: workspace.node_id,
      content, reviewer: document.getElementById('graph-reviewer')?.value.trim() || 'admin'
    });
    if (result.error) return log(`节点保存失败：${errorText(result)}`, 'error');
    await refreshBase();
    state.graphCandidateId = model.graph.object_id;
    render();
    log(`节点修改已保存为候选：${result.node_id}`, 'ok');
  });
}

async function loadNodeContentWorkspace(item, model) {
  const host = document.getElementById('node-content-workspace');
  if (!host || !model.graph) return;
  const nodeOrigin = item.candidateObjectId ? 'candidate' : 'baseline';
  const nodeId = item.candidateObjectId || item.sourceId;
  const query = new URLSearchParams({ task_id: currentTaskId(), graph_id: model.graph.object_id, node_origin: nodeOrigin, node_id: nodeId });
  const result = await api(`/node-content?${query}`);
  if (!document.getElementById('node-content-workspace')) return;
  if (result.error) { host.innerHTML = `<p class="danger-note">节点编辑器加载失败：${esc(errorText(result))}</p>`; return; }
  host.innerHTML = renderNodeContentWorkspace(result);
  bindNodeContentWorkspace(result, item, model);
}

function renderEdgeConditionWorkspace(workspace) {
  const disabled = workspace.editable ? '' : 'disabled';
  const issues = workspace.condition_issues || [];
  return `<section class="edge-condition-workspace">
    <div class="panel-title"><h4>路由条件人工校准</h4>${badge(workspace.condition_review_status || 'unreviewed', workspace.condition_review_status === 'confirmed' ? 'green' : workspace.review_required ? 'red' : 'amber')}</div>
    <p class="muted">只确认这段条件本身。模糊但有意义的描述可以保留；未提供的信息不会被解释成否定。</p>
    <label class="field"><span>当前规范路由条件</span><textarea class="textarea edge-condition-editor" data-saved="${esc(workspace.current_condition || '')}" ${disabled}>${esc(workspace.current_condition || '')}</textarea></label>
    <p class="edge-dirty-note" hidden>有未保存修改</p>
    <div class="actions edge-condition-actions"><button class="button button-primary save-edge-condition" ${disabled}>保存并确认条件</button><button class="button reset-edge-condition" ${disabled}>重置为原始条件</button></div>
    <dl class="detail-list edge-condition-meta"><div><dt>原始提炼条件</dt><dd>${esc(workspace.original_condition || '原始条件为空')}</dd></div>${workspace.condition_uncertainty ? `<div><dt>原始不确定性</dt><dd>${esc(workspace.condition_uncertainty)}</dd></div>` : ''}</dl>
    ${issues.length ? `<p class="danger-note">该边仍有 ${issues.length} 项问题，处理前会阻断整图批准和 Prompt 编译。</p>` : ''}
  </section>`;
}

function updateEdgeEditorDirty(host) {
  const editor = host.querySelector('.edge-condition-editor');
  if (!editor) return;
  const dirty = editor.value !== editor.dataset.saved;
  editor.dataset.dirty = dirty ? '1' : '0';
  editor.classList.toggle('is-dirty', dirty);
  const note = host.querySelector('.edge-dirty-note');
  if (note) note.hidden = !dirty;
  syncGraphApprovalDirtyState();
}

function bindEdgeConditionWorkspace(workspace, item, model) {
  const host = document.getElementById('edge-condition-workspace');
  if (!host) return;
  const editor = host.querySelector('.edge-condition-editor');
  if (!editor || !workspace.editable) return;
  editor.oninput = () => updateEdgeEditorDirty(host);
  host.querySelector('.reset-edge-condition').onclick = () => {
    editor.value = workspace.original_condition || '';
    updateEdgeEditorDirty(host);
  };
  host.querySelector('.save-edge-condition').onclick = event => runButton(event.currentTarget, '保存中...', async () => {
    const condition = editor.value.trim();
    if (!condition) return log('空条件不能伪装为已确认；请填写可用条件或暂不确认', 'error');
    const result = await post('/edge-condition', {
      task_id: currentTaskId(), graph_id: model.graph.object_id,
      edge_origin: workspace.edge_origin, edge_id: workspace.edge_id,
      condition, reviewer: document.getElementById('graph-reviewer')?.value.trim() || 'admin'
    });
    if (result.error) return log(`边条件保存失败：${errorText(result)}`, 'error');
    await refreshBase();
    state.graphCandidateId = model.graph.object_id;
    render();
    log(`边条件已确认：${result.edge_id}`, 'ok');
  });
}

async function loadEdgeConditionWorkspace(item, model) {
  const host = document.getElementById('edge-condition-workspace');
  if (!host || !model.graph) return;
  const edgeOrigin = item.candidateObjectId ? 'candidate' : 'baseline';
  const edgeId = item.candidateObjectId || item.sourceId;
  const query = new URLSearchParams({ task_id: currentTaskId(), graph_id: model.graph.object_id, edge_origin: edgeOrigin, edge_id: edgeId });
  const result = await api(`/edge-condition?${query}`);
  if (!document.getElementById('edge-condition-workspace')) return;
  if (result.error) { host.innerHTML = `<p class="danger-note">边条件加载失败：${esc(errorText(result))}</p>`; return; }
  host.innerHTML = renderEdgeConditionWorkspace(result);
  bindEdgeConditionWorkspace(result, item, model);
}

function graphDetail(item, model) {
  const node = document.getElementById('graph-detail');
  if (!node) return;
  if (node.querySelector('.script-editor[data-dirty="1"], .edge-condition-editor[data-dirty="1"], .node-content-editor[data-dirty="1"], .graph-layout-editor[data-dirty="1"]') && !window.confirm('右栏有未保存修改，是否放弃并切换对象？')) return;
  if (!item) { node.innerHTML = '<span class="muted">点击节点或边，核对结构证据、目标专家原话和回听时间</span>'; return; }
  const raw = item.raw || {};
  const structureEvidence = evidenceForRefs(item.evidence_refs);
  const contextEvidence = evidenceForRefs(item.context_refs);
  const scriptEvidence = evidenceForRefs(item.script_evidence_refs);
  const candidateScripts = item.candidateObjectId
    ? model.candidateScripts.filter(script => script.linkage?.node_id === item.candidateObjectId) : [];
  const baselineScripts = !raw.type ? (raw.scripts || []) : [];
  const typeLabel = item.kind === 'candidate' ? '本次变更' : item.kind === 'inherited' ? '候选图继承基线' : '既有基线';
  const changeType = item.diffType || item.changeType || 'unchanged';
  const isEdge = Boolean(item.source && item.kind !== 'match');
  const isNode = !isEdge && item.kind !== 'match';
  const conditionKindLabel = item.conditionKind === 'explicit' ? '显式条件' : item.conditionKind === 'implicit_sequence' ? '无条件顺序衔接（原图未填写显式条件）' : item.conditionKind === 'missing_branch_condition' ? '条件缺失：无法判断分支' : item.conditionKind === 'review_required_condition' ? '条件存在不确定性：需要人工确认' : item.conditionKind === 'conflicting_branch_condition' ? '条件冲突：同条件指向不同目标' : '';
  node.innerHTML = `<div class="panel-title"><h4>${esc(typeLabel)}</h4>${badge(changeType, changeType === 'add' ? 'green' : changeType === 'removed' ? 'red' : changeType.includes('change') || changeType === 'modify' || changeType === 'split' || changeType === 'merge' ? 'amber' : 'blue')}</div>
    <dl class="detail-list"><div><dt>名称 / 条件</dt><dd>${esc(item.label)}</dd></div>${conditionKindLabel ? `<div><dt>条件状态</dt><dd>${esc(conditionKindLabel)}</dd></div>` : ''}<div><dt>结构 ID</dt><dd class="mono">${esc(item.id)}</dd></div>${item.source ? `<div><dt>连接</dt><dd class="mono">${esc(item.source)} → ${esc(item.target)}</dd></div>` : ''}${item.baselineRefs?.length ? `<div><dt>精确基线</dt><dd class="mono">${esc(item.baselineRefs.join(', '))}</dd></div>` : ''}${item.changeReason ? `<div><dt>变更原因</dt><dd>${esc(item.changeReason)}</dd></div>` : ''}</dl>
    ${item.conditionKind === 'missing_branch_condition' ? '<p class="danger-note">同一节点存在多条出边，但这条边没有条件。当前无法判断何时走到这里，补齐前不能批准或编译。</p>' : ''}
    ${item.conditionKind === 'review_required_condition' ? '<p class="danger-note">模型明确标记了这条条件的不确定性。可保留模糊表述，但需人工确认后再批准或编译。</p>' : ''}
    ${isEdge && model.graph ? '<div id="edge-condition-workspace"><div class="empty">正在加载路由条件...</div></div>' : ''}
    ${isNode && model.graph ? '<div id="node-content-workspace"><div class="empty">正在加载节点编辑器...</div></div>' : ''}
    ${isEdge && model.graph ? renderEdgeLayoutEditor(item) : ''}
    ${isNode && model.graph ? renderNodeLayoutEditor(item, model) : ''}
    ${structureEvidence.length ? `<section class="evidence-section"><h4>结构依据 · ${structureEvidence.length}</h4><p class="muted">用于判断这个节点、边或条件是否应出现在 Graph 中。</p>${renderEvidenceCards(structureEvidence)}</section>` : ''}
    ${scriptEvidence.length ? `<section class="evidence-section script-evidence"><h4>目标专家原话 · ${scriptEvidence.length}</h4><p class="muted">初始证据保持不可变；可按时间位置回听原声，选择范例并保存人工校准版本。</p>${item.kind === 'candidate' && item.candidateObjectId && model.graph ? '<div id="script-workspace" class="script-workspace"><div class="empty">正在加载原话版本...</div></div>' : renderEvidenceCards(scriptEvidence, 'script')}</section>` : ''}
    ${contextEvidence.length ? `<details class="evidence-section"><summary>理解上下文 · ${contextEvidence.length}（不归因给目标专家）</summary>${renderEvidenceCards(contextEvidence, 'context')}</details>` : ''}
    ${baselineScripts.length || candidateScripts.length ? `<section class="graph-script-list"><h4>已有关联话术</h4>${baselineScripts.map(script => `<div class="relation">${esc(script.text || '')}<small>${esc(script.source || script.script_type || '基线导入')}</small></div>`).join('')}${candidateScripts.map(script => `<div class="relation">${esc(script.content)}<small>${esc(script.object_id)}</small></div>`).join('')}</section>` : ''}
    ${!structureEvidence.length && !scriptEvidence.length && item.kind === 'candidate' ? '<p class="danger-note">该候选对象没有可展示的证据回链，不应批准。</p>' : ''}
    <details><summary>工程字段</summary><pre class="json-preview">${esc(JSON.stringify(raw, null, 2))}</pre></details>`;
  if (isEdge && model.graph) loadEdgeConditionWorkspace(item, model);
  if (isNode && model.graph) loadNodeContentWorkspace(item, model);
  if ((isEdge || isNode) && model.graph) bindLayoutEditor(item, model);
  if (scriptEvidence.length && item.kind === 'candidate' && item.candidateObjectId && model.graph) loadScriptWorkspace(item, model);
}

function renderGraphSvg(model) {
  const canvas = document.getElementById('graph-canvas');
  if (!canvas) return;
  const { nodes, edges } = graphElements(model);
  const width = Math.max(720, canvas.clientWidth || 720);
  const columns = Math.max(1, Math.ceil(Math.sqrt(nodes.length || 1)));
  const cardWidth = Math.min(210, Math.max(140, (width - 40) / columns - 14));
  const cardHeight = 62;
  const positions = new Map(nodes.map((node, index) => [node.id, {
    x: 20 + (index % columns) * (cardWidth + 14), y: 20 + Math.floor(index / columns) * (cardHeight + 28)
  }]));
  const height = Math.max(260, 40 + Math.ceil((nodes.length || 1) / columns) * (cardHeight + 28));
  const lines = edges.map(edge => { const from = positions.get(edge.source); const to = positions.get(edge.target); return from && to ? `<g class="graph-svg-edge-group" data-graph-edge-id="${esc(edge.id)}"><line x1="${from.x + cardWidth / 2}" y1="${from.y + cardHeight}" x2="${to.x + cardWidth / 2}" y2="${to.y}" class="graph-svg-edge"/><text x="${(from.x + to.x + cardWidth) / 2}" y="${(from.y + to.y + cardHeight) / 2}" class="graph-svg-label">${esc(short(edge.label, 22))}</text></g>` : ''; }).join('');
  const cards = nodes.map(node => { const p = positions.get(node.id); return `<g class="graph-svg-node" data-graph-id="${esc(node.id)}"><rect x="${p.x}" y="${p.y}" width="${cardWidth}" height="${cardHeight}" rx="5"/><text x="${p.x + 10}" y="${p.y + 23}">${esc(short(node.label, 26))}</text><text x="${p.x + 10}" y="${p.y + 44}" class="graph-svg-meta">${esc(node.kind)} · ${esc(node.status)}</text></g>`; }).join('');
  canvas.innerHTML = `<svg class="graph-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Graph 结构图"><defs><marker id="graph-arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#718097"/></marker></defs><g>${lines}</g>${cards}</svg>`;
  canvas.querySelectorAll('[data-graph-id]').forEach(item => item.onclick = () => graphDetail(nodes.find(node => node.id === item.dataset.graphId), model));
  canvas.querySelectorAll('[data-graph-edge-id]').forEach(item => item.onclick = () => graphDetail(edges.find(edge => edge.id === item.dataset.graphEdgeId), model));
}

function renderGraphCanvas() {
  const model = graphViewModel();
  const canvas = document.getElementById('graph-canvas');
  if (!canvas) return;
  const { nodes, edges } = graphElements(model);
  if (!nodes.length) { canvas.innerHTML = '<div class="empty">当前模式没有可展示的 Graph 结构</div>'; graphDetail(null, model); return; }
  if (!window.cytoscape) { renderGraphSvg(model); return; }
  if (window.cytoscapeElk && !window.__graphElkRegistered) { window.cytoscape.use(window.cytoscapeElk); window.__graphElkRegistered = true; }
  const useCallFlow = state.graphLayout === 'call_flow' && ['ready', 'partial'].includes(state.graphLayoutProfile?.status);
  const flow = useCallFlow ? callFlowLayout(nodes, edges, state.graphLayoutProfile) : null;
  const elements = [
    ...(flow?.guides || []),
    ...nodes.map(node => ({ data: { id: node.id, label: node.label, kind: node.kind, status: node.status, diffType: node.diffType || node.changeType || 'unchanged', strategy: '1', item: node }, ...(flow ? { position: flow.positions.get(node.id) } : {}) })),
    ...edges.map(edge => {
      const route = flow?.edgeRoutes?.get(edge.id) || {};
      return { data: { id: edge.id, source: edge.source, target: edge.target, label: edge.label, conditionKind: edge.conditionKind || '', kind: edge.kind, diffType: edge.diffType || edge.changeType || 'unchanged', strategy: '1', item: edge, curveDistance: route.curveDistance || 24, curveWeight: route.curveWeight || 0.5 }, classes: [edge.source === edge.target ? 'self-loop' : '', edge.kind === 'match' ? 'match-edge' : '', flow && edge.kind !== 'match' ? 'flow-edge' : '', flow?.backEdgeIds.has(edge.id) ? 'back-edge' : ''].filter(Boolean).join(' ') };
    })
  ];
  canvas.innerHTML = '';
  const layout = { name: flow || state.graphLayout === 'original' ? 'preset' : state.graphLayout === 'grid' ? 'grid' : window.cytoscapeElk ? 'elk' : 'breadthfirst', fit: !flow, padding: 30 };
  if (state.graphLayout === 'original') layout.positions = node => node.data('item').position || { x: 100 + node.index() * 20, y: 100 + node.index() * 20 };
  if (window.cytoscapeElk && !flow && state.graphLayout !== 'grid' && state.graphLayout !== 'original') layout.elk = { algorithm: 'layered', 'elk.direction': 'DOWN', 'elk.spacing.nodeNode': 45, 'elk.layered.spacing.nodeNodeBetweenLayers': 65 };
  try {
  const cy = window.cytoscape({ container: canvas, elements, style: [
    { selector: 'node[strategy = "1"]', style: { 'background-color': ele => ({ add: '#238a68', modify: '#a8732c', split: '#a8732c', merge: '#a8732c', removed: '#873f47', 'changed-old': '#665036', unchanged: '#354b70', keep: '#354b70' }[ele.data('diffType')] || '#2f72c7'), 'label': 'data(label)', color: '#e8edf5', 'text-wrap': 'wrap', 'text-max-width': 160, 'font-size': 12, 'text-valign': 'center', 'text-halign': 'center', width: 164, height: 58, 'border-width': 2, 'border-color': ele => ele.data('status') === 'approved' ? '#2ec98c' : '#70839d' } },
    { selector: 'node[guide = "phase"]', style: { 'shape': 'roundrectangle', width: 'data(guideWidth)', height: 'data(guideHeight)', 'background-color': '#17202b', 'background-opacity': 0.5, 'border-color': '#344154', 'border-width': 1, label: 'data(label)', color: '#8fa1ba', 'font-size': 16, 'font-weight': 700, 'text-halign': 'left', 'text-valign': 'top', 'text-margin-x': 18, 'text-margin-y': 12, 'text-background-opacity': 0, 'events': 'no', 'z-index': -10 } },
    { selector: 'edge', style: { width: ele => ele.data('diffType') === 'match' ? 1 : 2, 'line-color': ele => ({ add: '#2ec98c', modify: '#e6aa55', split: '#e6aa55', merge: '#e6aa55', removed: '#ed6b6b', 'changed-old': '#9a7445', match: '#687589', unchanged: '#6c7890' }[ele.data('diffType')] || '#4d8df7'), 'target-arrow-color': ele => ele.data('diffType') === 'removed' ? '#ed6b6b' : '#718097', 'target-arrow-shape': ele => ele.data('diffType') === 'match' ? 'none' : 'triangle', 'curve-style': 'bezier', label: '', color: '#d7e0ee', 'font-size': 11, 'text-wrap': 'wrap', 'text-max-width': 260, 'text-background-color': '#101318', 'text-background-opacity': 0.94, 'text-background-padding': 4, opacity: 0.62 } },
    { selector: 'edge.flow-edge', style: { 'curve-style': 'unbundled-bezier', 'control-point-distances': 'data(curveDistance)', 'control-point-weights': 'data(curveWeight)' } },
    { selector: 'edge.back-edge', style: { 'line-style': 'dashed', 'control-point-distances': 'data(curveDistance)', 'control-point-weights': 'data(curveWeight)' } },
    { selector: 'edge.match-edge', style: { 'line-style': 'dashed', 'curve-style': 'straight', opacity: 0.75 } },
    { selector: 'edge[conditionKind = "missing_branch_condition"]', style: { 'line-color': '#ed6b6b', 'target-arrow-color': '#ed6b6b', color: '#ff8a8a', width: 4 } },
    { selector: 'edge[conditionKind = "review_required_condition"]', style: { 'line-color': '#ed6b6b', 'target-arrow-color': '#ed6b6b', color: '#ff8a8a', width: 4 } },
    { selector: 'edge[conditionKind = "conflicting_branch_condition"]', style: { 'line-color': '#ed6b6b', 'target-arrow-color': '#ed6b6b', color: '#ff8a8a', width: 4 } },
    { selector: 'edge.self-loop', style: { 'curve-style': 'unbundled-bezier', 'loop-direction': '-45deg', 'loop-sweep': '-90deg', 'control-point-distances': 60 } },
    { selector: '.context-muted', style: { opacity: 0.1, 'text-opacity': 0.08 } },
    { selector: 'node.context-muted', style: { opacity: 0.18, 'text-opacity': 0.18 } },
    { selector: 'edge.focus-edge, edge.hover-edge', style: { label: 'data(label)', opacity: 1, 'text-opacity': 1, width: 4, 'z-index': 20 } },
    { selector: 'node.focus-node', style: { opacity: 1, 'text-opacity': 1, 'border-color': '#e6aa55', 'border-width': 4, 'z-index': 21 } },
    { selector: ':selected', style: { 'border-color': '#e6aa55', 'border-width': 4, 'line-color': '#e6aa55', 'target-arrow-color': '#e6aa55' } }
  ], layout });
  if (flow) {
    const readableZoom = Math.min(1, Math.max(0.72, (canvas.clientWidth - 36) / flow.width));
    cy.zoom(readableZoom);
    cy.pan({ x: canvas.clientWidth / 2 - flow.focusX * readableZoom, y: 26 });
  }
  const clearGraphFocus = () => {
    cy.elements('[strategy = "1"]').removeClass('context-muted focus-edge focus-node');
  };
  const focusGraphItem = item => {
    clearGraphFocus();
    const strategyElements = cy.elements('[strategy = "1"]');
    strategyElements.addClass('context-muted');
    if (item.isNode()) {
      const relatedEdges = item.connectedEdges('[strategy = "1"]');
      item.removeClass('context-muted').addClass('focus-node');
      relatedEdges.removeClass('context-muted').addClass('focus-edge');
      relatedEdges.connectedNodes('[strategy = "1"]').removeClass('context-muted');
    } else {
      item.removeClass('context-muted').addClass('focus-edge');
      item.connectedNodes('[strategy = "1"]').removeClass('context-muted').addClass('focus-node');
    }
  };
  cy.on('tap', '[strategy = "1"]', event => {
    focusGraphItem(event.target);
    graphDetail(event.target.data('item'), model);
  });
  cy.on('mouseover', 'edge[strategy = "1"]', event => event.target.addClass('hover-edge'));
  cy.on('mouseout', 'edge[strategy = "1"]', event => event.target.removeClass('hover-edge'));
  let positionSaveQueue = Promise.resolve();
  cy.on('dragfree', 'node[strategy = "1"]', event => {
    if (!flow || !model.graph || !state.graphLayoutProfile) return;
    const node = event.target;
    const displayId = node.id();
    const nodeId = graphItemLayoutId(node.data('item'));
    const position = { x: node.position('x'), y: node.position('y') };
    const previous = { ...(flow.positions.get(displayId) || position) };
    const graphId = model.graph.object_id;
    const taskId = currentTaskId();
    const graphHash = state.graphLayoutProfile.materialized_graph_hash;
    positionSaveQueue = positionSaveQueue.then(async () => {
      const result = await post('/graph-layout', {
        task_id: taskId, graph_id: graphId, materialized_graph_hash: graphHash,
        position_updates: [{ node_id: nodeId, ...position }],
        editor: document.getElementById('graph-reviewer')?.value.trim() || 'admin',
      });
      if (result.error) {
        if (canvas.__cy === cy && state.graphCandidateId === graphId) node.position(previous);
        return log(`节点位置自动保存失败：${errorText(result)}`, 'error');
      }
      flow.positions.set(displayId, position);
      if (state.graphCandidateId === graphId) state.graphLayoutProfile = result;
      log('节点位置已自动保存', 'ok');
    });
  });
  cy.on('tap', event => {
    if (event.target === cy) {
      clearGraphFocus();
      graphDetail(null, model);
    }
  });
  document.getElementById('graph-fit')?.addEventListener('click', () => cy.fit(undefined, 30));
  document.getElementById('graph-zoom-in')?.addEventListener('click', () => cy.zoom({ level: cy.zoom() * 1.2, renderedPosition: { x: canvas.clientWidth / 2, y: canvas.clientHeight / 2 } }));
  document.getElementById('graph-zoom-out')?.addEventListener('click', () => cy.zoom({ level: cy.zoom() / 1.2, renderedPosition: { x: canvas.clientWidth / 2, y: canvas.clientHeight / 2 } }));
  canvas.__cy = cy;
  window.__activeGraph = cy;
  } catch (error) {
    canvas.insertAdjacentHTML('beforeend', `<div class="graph-render-error">Graph 渲染异常：${esc(error.message)}</div>`);
    console.error('Graph render failed', error);
  }
}
function renderKnowledge() {
  const model = graphViewModel();
  const materialized = materializedCandidate(model);
  const graph = model.graph;
  const duplicateNotice = (state.hiddenDuplicateKnowledge || state.possibleDuplicateKnowledge) ? `<p class="muted">后台已收纳 ${state.hiddenDuplicateKnowledge} 个完全重复候选；另有 ${state.possibleDuplicateKnowledge} 个共享证据但结构不同的工程候选。</p>` : '';
  const candidateOptions = model.graphRecords.length
    ? model.graphRecords.map((item, index) => `<option value="${esc(item.object_id)}" ${item.object_id === graph?.object_id ? 'selected' : ''}>候选 ${index + 1} · ${esc(item.created_at || '')} · ${esc(item.status)}</option>`).join('')
    : '<option value="">暂无候选 Graph</option>';
  const baselineOptions = state.baselines.length
    ? state.baselines.map(baseline => `<option value="${esc(baseline.baseline_id)}" ${model.baselineId === baseline.baseline_id ? 'selected' : ''}>${esc(baseline.name)} · v${esc(baseline.version)} · ${esc(short(baseline.content_hash, 10))}</option>`).join('')
    : '<option value="">暂无基线 Graph</option>';
  const lockedBaseline = Boolean(graph?.linkage?.baseline_id);
  const summary = graph?.linkage?.analysis_summary || '旧候选未记录增量分析摘要；系统已按 baseline_match 尽力物化。';
  const uncertainties = graph?.linkage?.uncertainties || [];
  const rejectedChanges = graph?.linkage?.rejected_changes || [];
  const rejectionNotice = rejectedChanges.length
    ? `<div class="notice notice-red">有 ${rejectedChanges.length} 项模型变更未通过服务端校验，当前候选不可批准：${esc(rejectedChanges.map(item => `${item.entity_type || 'change'}:${item.reason || 'rejected'}`).join('；'))}</div>`
    : '';
  const conditionIssues = materialized.conditionIssues || [];
  const conditionNotice = renderConditionIssueNotice(conditionIssues, materialized);
  const layoutStatus = state.graphLayoutProfile?.status || 'missing';
  const layoutStatusLabel = ({ ready: '流程布局已缓存', partial: '流程布局部分可用', stale: '流程布局待更新', failed: '流程布局分析失败', running: '流程布局分析中', missing: '流程布局待生成' })[layoutStatus] || layoutStatus;
  const layoutStatusTone = layoutStatus === 'ready' ? 'green' : layoutStatus === 'failed' ? 'red' : 'amber';
  return `${viewHeading('<button class="button" id="knowledge-refresh">刷新</button>')}
    <div class="panel graph-audit-panel"><div class="panel-title"><div><h3>一张 Graph，三种审查模式</h3><p class="muted">候选模式展示“完整基线 + 本次增量”物化结果；既有模式只展示输入基线；差异模式在同一结构上标色。</p></div>${graph ? badge(graph.status, graph.status === 'approved' ? 'green' : 'amber') : badge('无候选', 'red')}</div>
      <div class="graph-toolbar"><label class="field graph-candidate-field"><span>候选版本</span><select class="select" id="graph-candidate">${candidateOptions}</select></label><label class="field"><span>画布模式</span><select class="select" id="graph-mode"><option value="candidate" ${state.graphMode === 'candidate' ? 'selected' : ''}>候选图（基线 + 增量）</option><option value="reference" ${state.graphMode === 'reference' ? 'selected' : ''} ${!model.baseline ? 'disabled' : ''}>既有基线</option><option value="diff" ${state.graphMode === 'diff' ? 'selected' : ''} ${!model.baseline ? 'disabled' : ''}>差异标色</option></select></label><label class="field"><span>精确基线${lockedBaseline ? '（候选已锁定）' : ''}</span><select class="select" id="graph-baseline" ${!state.baselines.length || lockedBaseline ? 'disabled' : ''}>${baselineOptions}</select></label><label class="field"><span>布局</span><select class="select" id="graph-layout-select"><option value="call_flow" ${state.graphLayout === 'call_flow' ? 'selected' : ''}>电话流程（七阶段）</option><option value="auto" ${state.graphLayout === 'auto' ? 'selected' : ''}>拓扑自动分层</option><option value="original" ${state.graphLayout === 'original' ? 'selected' : ''}>保留原始坐标</option><option value="grid" ${state.graphLayout === 'grid' ? 'selected' : ''}>网格</option></select></label><div class="graph-layout-status">${badge(layoutStatusLabel, layoutStatusTone)}<button class="button" id="graph-layout-analyze" ${graph && layoutStatus !== 'running' ? '' : 'disabled'}>${layoutStatus === 'failed' ? '重试布局分析' : '重新分析布局'}</button></div><div class="graph-toolbar-actions"><button class="button" id="graph-layout-reset" ${graph ? '' : 'disabled'} title="清空拖动保存的坐标，按阶段与分支规则重新排版">重新初始化布局</button><button class="button" id="graph-export" ${graph ? '' : 'disabled'}>导出当前 Graph JSON</button><button class="button" id="graph-fit">适配</button><button class="button" id="graph-zoom-out" title="缩小">−</button><button class="button" id="graph-zoom-in" title="放大">+</button></div></div>
      <div class="graph-run-summary"><div><span>物化候选</span><strong>${materialized.nodes.length} 节点 · ${materialized.edges.length} 条边</strong></div><div><span>本次变更</span><strong>${model.candidateNodes.length} 节点 · ${model.candidateEdges.length} 条边</strong></div><div><span>基线身份</span><strong class="mono">${esc(model.baselineId || '未绑定')}</strong></div></div>
      <div class="graph-stage"><div id="graph-canvas" class="graph-canvas"><div class="empty">正在加载 Graph 结构...</div></div><aside id="graph-detail" class="graph-detail"><span class="muted">点击节点或边，核对结构证据、目标专家原话和回听时间</span></aside></div>
      <div class="graph-legend"><span><i class="legend-dot legend-baseline"></i>基线未变</span><span><i class="legend-dot legend-modified"></i>修改 / 拆分 / 合并</span><span><i class="legend-dot legend-added"></i>新增</span><span><i class="legend-dot legend-removed"></i>废弃</span></div>
      <div class="graph-review-summary"><h4>模型增量判断</h4><p>${esc(summary)}</p>${rejectionNotice}${conditionNotice}${uncertainties.length ? `<div class="notice notice-amber">待确认：${esc(uncertainties.join('；'))}</div>` : '<p class="success-note">模型未报告额外不确定项；仍应点击关键变化逐项核对证据。</p>'}</div>
      ${graph ? `<div class="graph-review-actions"><label class="field"><span>审核人</span><input class="input" id="graph-reviewer" value="admin"></label><label class="field graph-review-reason"><span>整图审核说明</span><input class="input" id="graph-review-reason" value="已在 Graph 上核对结构变化、原话证据与时间位置"></label><div class="actions"><button class="button button-primary" id="approve-graph" ${graph.status === 'approved' || rejectedChanges.length || conditionIssues.length ? 'disabled' : ''}>批准整张候选图</button><button class="button button-danger" id="reject-graph" ${graph.status === 'approved' ? 'disabled' : ''}>驳回整张候选图</button></div></div>` : '<div class="empty">请先在“任务与专家”运行完整访谈增量学习。</div>'}
      ${duplicateNotice}<details class="engineering-details"><summary>工程对象与原始 JSON（排障用）</summary><pre class="json-preview">${esc(JSON.stringify({ graph, changes: [...model.candidateNodes, ...model.candidateEdges].map(item => item.raw) }, null, 2))}</pre></details></div>${renderLog()}`;
}

function renderRelease() {
  const compilation = state.selectedCompilation;
  const release = state.selectedRelease;
  const approvedGraph = state.knowledge.find(k => k.type === 'graph' && k.status === 'approved');
  const gateReady = ['G4', 'G5', 'published'].includes(currentGateLabel());
  const canGenerateExecution = Boolean(approvedGraph && gateReady && state.config.api_key_configured);
  const canCompile = Boolean(approvedGraph && gateReady && state.config.api_key_configured);
  const compileBlock = !approvedGraph ? '先完成完整 Graph 的 G3 审核' : !gateReady ? `当前待办 ${currentGateLabel()}，尚未完成对象级 G3` : !state.config.api_key_configured ? '尚未配置可用 API Key' : '';
  return `${viewHeading('<button class="button" id="release-refresh">刷新</button>')}
    <div class="layout"><div>
      <div class="panel"><div class="panel-title"><h3>编译控制</h3>${badge(canCompile ? '可编译' : '已阻断', canCompile ? 'green' : 'red')}</div><div class="actions"><button class="button" id="generate-execution" ${canGenerateExecution ? '' : 'disabled'}>LLM 生成电话执行 Prompt</button><button class="button" id="generate-strategy" ${canCompile ? '' : 'disabled'}>生成策略评价 Prompt</button><button class="button" id="generate-script" ${canCompile ? '' : 'disabled'}>生成话术评价 Prompt</button><button class="button button-primary" id="compile-prompts" ${canCompile ? '' : 'disabled'}>编译执行 + 评价 Prompt</button></div><p class="muted" style="margin-top:12px">${esc(compileBlock || 'LLM 负责生成执行说明层；节点、话术、Trigger、停止条件与权威路由表由后端无损追加。')}</p></div>
      <div class="panel"><div class="panel-title"><h3>编译产物</h3>${badge(`${state.compilations.length} 个`, 'blue')}</div>${compilation ? `<dl class="detail-list"><div><dt>compile_id</dt><dd class="mono">${esc(compilation.compile_id)}</dd></div><div><dt>输入对象</dt><dd>${esc(compilation.manifest?.input_object_count || 0)}</dd></div><div><dt>评价 Prompt 哈希</dt><dd class="mono">${esc(short(compilation.manifest?.combined_prompt_sha256, 28))}</dd></div><div><dt>执行 Prompt 哈希</dt><dd class="mono">${esc(short(compilation.manifest?.execution_prompt_sha256, 28))}</dd></div><div><dt>路由表哈希</dt><dd class="mono">${esc(short(compilation.manifest?.route_table_sha256, 28))}</dd></div><div><dt>编译器</dt><dd>${esc(compilation.manifest?.compiler_version)}</dd></div></dl><h4>电话执行 Prompt</h4><div class="prompt-preview">${esc(compilation.execution_prompt || '旧编译记录没有电话执行 Prompt')}</div><h4 style="margin-top:12px">综合评价 Prompt</h4><div class="prompt-preview">${esc(compilation.combined_prompt || '从列表加载完整产物后显示')}</div>` : '<div class="empty">暂无编译产物</div>'}</div>
    </div><div>
      <div class="panel"><div class="panel-title"><h3>发布 Gate</h3>${badge(approvedGate('G5') ? 'G5 已通过' : `当前待办 ${currentGateLabel()}`, approvedGate('G5') ? 'green' : 'amber')}</div><div class="form-grid"><label class="field"><span>发布责任人</span><input class="input" id="release-owner" value="admin"></label><label class="field"><span>确认原因</span><input class="input" id="release-reason" value="Prompt 与哈希复核通过"></label></div><div class="actions" style="margin-top:12px"><button class="button" id="approve-g4" ${currentGateLabel() === 'G4' ? '' : 'disabled'}>通过 G4（适用时）</button><button class="button button-primary" id="approve-g5" ${currentGateLabel() === 'G5' ? '' : 'disabled'}>通过 G5</button></div></div>
      <div class="panel"><div class="panel-title"><h3>发布与交付</h3>${release ? badge(release.status, 'green') : badge('未发布', 'amber')}</div>${release ? `<dl class="detail-list"><div><dt>release_id</dt><dd class="mono">${esc(release.release_id)}</dd></div><div><dt>版本</dt><dd>v${esc(release.version)}</dd></div><div><dt>Prompt 哈希</dt><dd class="mono">${esc(short(release.release_manifest?.prompt_sha256, 28))}</dd></div></dl><div class="actions"><button class="button" id="export-release">导出 JSON</button><button class="button" id="record-delivery">记录完整性交付</button></div>` : `<button class="button button-primary" id="create-release" ${!compilation ? 'disabled' : ''}>从当前编译创建发布包</button>`}</div>
    </div></div>${renderLog()}`;
}

function renderAudit() {
  return `${viewHeading('<button class="button" id="audit-refresh">刷新</button>')}<div class="layout"><div class="panel" id="audit-events"><div class="empty">正在加载审计事件...</div></div><div class="panel"><div class="panel-title"><h3>LLM 配置</h3>${badge(state.config.api_key_configured ? '外部 Key 已配置' : '未配置 Key', state.config.api_key_configured ? 'green' : 'amber')}</div><div class="form-grid"><label class="field field-full"><span>Base URL</span><input class="input" id="cfg-url" value="${esc(state.config.base_url || '')}"></label><label class="field"><span>模型</span><input class="input" id="cfg-model" value="${esc(state.config.model || '')}"></label><label class="field"><span>Temperature</span><input class="input" id="cfg-temp" type="number" step="0.1" value="${esc(state.config.temperature ?? 0.7)}"></label><label class="field"><span>单次输出上限</span><input class="input" id="cfg-tokens" type="number" value="${esc(state.config.max_tokens || 65536)}"></label><label class="field"><span>超时（秒）</span><input class="input" value="${esc(state.config.timeout || 900)}" readonly></label><label class="field"><span>思考模式</span><input class="input" value="${esc(state.config.thinking?.type || 'enabled')}" readonly></label><input id="cfg-utts" type="hidden" value="${esc(state.config.max_utterances_per_call || 10)}"><label class="field field-full"><span>当前服务进程的新 API Key</span><input class="input" id="cfg-key" type="password" placeholder="留空不变；不会写入项目文件"></label></div><div class="actions" style="margin-top:12px"><button class="button button-primary" id="save-config">保存非敏感配置</button><button class="button" id="test-config">测试连接</button></div><p class="muted">正式 Graph 学习固定读取完整访谈与完整基线，不使用发言条数限制；输出上限 65,536、深度思考和 900 秒超时作为质量基线。长期 Key 请用环境变量 AI_CALL_EVAL_API_KEY。</p></div></div>${renderLog()}`;
}

function render() {
  const renderers = { workspace: renderWorkspace, tasks: renderTasks, evidence: renderEvidence, knowledge: renderKnowledge, release: renderRelease, audit: renderAudit };
  root.innerHTML = renderers[state.view]();
  bindView();
}

async function runButton(button, pendingText, action) {
  if (!button) return;
  const original = button.textContent;
  button.disabled = true; button.textContent = pendingText;
  try { await action(); } finally { if (button.isConnected) { button.disabled = false; button.textContent = original; } }
}
async function reloadAndRender(message = '') {
  await refreshBase();
  if (message) log(message, 'ok');
  render();
}

function bindCommon() {
  root.querySelectorAll('[data-go]').forEach(button => button.onclick = () => go(button.dataset.go));
  root.querySelectorAll('tr[data-id]').forEach(row => row.onclick = event => {
    if (event.target.matches('input')) return;
    const id = row.dataset.id;
    if (state.view === 'tasks') {
      state.currentTask = state.tasks.find(t => t.task_id === id); state.selectedEvidence = null; state.selectedKnowledge = null;
      refreshBase().then(render);
    } else if (state.view === 'evidence') { state.selectedEvidence = state.evidence.find(e => e.evidence_id === id); render(); }
    else if (state.view === 'knowledge') { state.selectedKnowledge = state.knowledge.find(k => k.object_id === id); render(); }
  });
}

function bindView() {
  bindCommon();
  if (state.view === 'workspace') {
    const button = document.getElementById('workspace-next');
    if (button) button.onclick = () => go(!approvedGate('G1') ? 'tasks' : 'knowledge');
  }
  if (state.view === 'tasks') bindTasks();
  if (state.view === 'evidence') bindEvidence();
  if (state.view === 'knowledge') bindKnowledge();
  if (state.view === 'release') bindRelease();
  if (state.view === 'audit') bindAudit();
}

function bindTasks() {
  document.getElementById('refresh-tasks').onclick = () => reloadAndRender('任务数据已刷新');
  root.querySelectorAll('.delete-task').forEach(button => button.onclick = event => {
    event.stopPropagation();
    const taskId = button.dataset.taskId;
    if (!confirm(`永久删除任务“${button.dataset.taskLabel}”及其全部 Graph、证据和分析记录？\n\n${taskId}`)) return;
    runButton(button, '删除中...', async () => {
      const result = await post('/tasks/delete', { task_id: taskId, confirm_task_id: taskId, actor: 'admin' });
      if (result.error) return log(`任务删除失败：${errorText(result)}`, 'error');
      if (state.currentTask?.task_id === taskId) state.currentTask = null;
      state.selectedEvidence = null; state.selectedKnowledge = null; state.selectedEvidenceIds.clear();
      state.graphCandidateId = null; state.graphLayoutProfile = null;
      await reloadAndRender(`任务已删除：${taskId}`);
    });
  });
  document.getElementById('rerun-task').onclick = event => runButton(event.currentTarget, '新建中...', async () => {
    const result = await post('/tasks/rerun', { task_id: currentTaskId() });
    if (result.error) return log(`重跑任务创建失败：${errorText(result)}`, 'error');
    state.currentTask = result.task;
    state.selectedEvidence = null; state.selectedKnowledge = null; state.selectedEvidenceIds.clear();
    await reloadAndRender(`已从原来源新建重跑任务：${result.task.task_id}`);
  });
  document.getElementById('choose-txt').onclick = () => document.getElementById('local-txt').click();
  document.getElementById('local-txt').onchange = event => {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.txt')) return log('请选择 TXT 文件', 'error');
    runButton(document.getElementById('choose-txt'), '上传中...', async () => {
      const content = await file.text();
      const result = await post('/import-content', { filename: file.name, content });
      if (result.error === 'duplicate') return log(`文件已存在：${result.task_id}`, 'info');
      if (result.error) return log(`本地 TXT 导入失败：${errorText(result)}`, 'error');
      state.currentTask = result.task;
      await reloadAndRender(`本地 TXT 导入完成：${result.task.task_id}`);
    });
    event.target.value = '';
  };
  document.getElementById('load-files').onclick = async () => {
    const result = await api('/input-files');
    const node = document.getElementById('file-list');
    node.className = '';
    node.innerHTML = (result.files || []).map(file => `<button class="button button-ghost import-file" data-file="${esc(file.filename)}">导入 ${esc(file.filename)} · ${(file.size / 1024).toFixed(1)} KB</button>`).join('') || '<div class="empty">输入目录没有 TXT 文件</div>';
    node.querySelectorAll('.import-file').forEach(button => button.onclick = () => runButton(button, '导入中...', async () => {
      const imported = await post('/import', { filename: button.dataset.file });
      if (imported.error === 'duplicate') return log(`文件已存在：${imported.task_id}`, 'info');
      if (imported.error) return log(`导入失败：${errorText(imported)}`, 'error');
      state.currentTask = imported.task;
      await reloadAndRender(`导入完成：${imported.task.task_id}`);
    }));
  };
  document.getElementById('approve-g1').onclick = event => runButton(event.currentTarget, '提交中...', async () => {
    const baselineSelect = document.getElementById('g1-baseline-select');
    const baseline_id = baselineSelect ? baselineSelect.value : '';
    const result = await post('/gate', { gate_id: 'G1', task_id: currentTaskId(), reviewer: document.getElementById('g1-reviewer').value.trim(), decision: 'approved', reason: document.getElementById('g1-reason').value.trim(), target_expert: document.getElementById('target-expert').value.trim(), baseline_id });
    if (result.error) return log(`G1 阻断：${errorText(result)}`, 'error');
    await reloadAndRender(`G1 已通过：${result.audit_id}`);
  });
  const drawioBtn = document.getElementById('g1-import-drawio');
  const drawioFile = document.getElementById('g1-drawio-file');
  if (drawioBtn && drawioFile) {
    drawioBtn.onclick = () => drawioFile.click();
    drawioFile.onchange = event => {
      const file = event.target.files?.[0];
      event.target.value = '';
      if (!file) return;
      runButton(drawioBtn, '解析中...', async () => {
        const parsed = await post('/graph-baselines/parse', { filename: file.name, content: await file.text() });
        if (parsed.error) return log(`Graph 解析失败：${errorText(parsed)}`, 'error');
        const isJson = file.name.toLowerCase().endsWith('.json');
        const parsedGraph = parsed.graph || parsed;
        const saved = await post('/graph-baselines', { task_id: currentTaskId(), source_id: currentSourceId(), name: file.name.replace(/\.(drawio|xml|json)$/i, ''), origin: isJson ? 'portable_json' : 'drawio', source_filename: file.name, graph: parsedGraph, layout_profile: parsed.layout_profile || null });
        if (saved.error) return log(`Graph 保存失败：${errorText(saved)}`, 'error');
        const layoutNote = (parsed.warnings || []).length ? `；布局有 ${parsed.warnings.length} 项警告，策略图仍已导入` : parsed.layout_profile ? '；已恢复电话流程布局' : '';
        await reloadAndRender(`已导入基线 Graph：${file.name}（${parsedGraph.nodes?.length || 0} 节点，${parsedGraph.edges?.length || 0} 条边${layoutNote}）`);
      });
    };
  }
  const scriptBtn = document.getElementById('g1-import-scripts');
  const scriptFile = document.getElementById('g1-script-file');
  if (scriptBtn && scriptFile) {
    scriptBtn.onclick = () => scriptFile.click();
    scriptFile.onchange = event => {
      const file = event.target.files?.[0];
      event.target.value = '';
      if (!file) return;
      runButton(scriptBtn, 'LLM 映射中...', async () => {
        const text = await file.text();
        const baselineId = document.getElementById('g1-baseline-select')?.value || state.currentTask?.baseline_id || '';
        const result = await post('/script-documents/parse', { task_id: currentTaskId(), baseline_id: baselineId, filename: file.name, content: text });
        if (result.error) return log(`话术导入失败：${errorText(result)}`, 'error');
        await reloadAndRender(`话术映射候选已生成：${file.name}（${result.total_mappings} 条，候选 ${result.candidate_id}；基线未修改）`);
      });
    };
  }
  [['extract-strategy','/llm-extract-strategy','Graph 增量学习'],['map-scripts','/llm-map-scripts','话术映射']].forEach(([id,path,label]) => {
    const button = document.getElementById(id);
    if (button) button.onclick = () => runButton(button, 'LLM 处理中...', async () => {
      const result = await post(path, { source_id: currentSourceId(), ...(id === 'extract-strategy' ? { include_all: true } : {}) });
      if (result.error) return log(`${label}失败：${errorText(result)}`, 'error');
      if (id === 'extract-strategy') {
        await refreshBase();
        state.graphCandidateId = result.graph_id;
        state.selectedKnowledge = state.knowledge.find(item => item.object_id === result.graph_id) || null;
        state.view = 'knowledge';
        log(`${label}完成：完整读取 ${result.effective_max_utts} 条发言，模型 ${result.llm_model || state.config.model}`, 'ok');
        return switchView('knowledge');
      }
      await reloadAndRender(`${label}完成，模型 ${result.llm_model || state.config.model}`);
    });
  });
}

function bindEvidence() {
  root.querySelectorAll('.evidence-check').forEach(box => box.onchange = () => box.checked ? state.selectedEvidenceIds.add(box.dataset.eid) : state.selectedEvidenceIds.delete(box.dataset.eid));
  document.getElementById('select-all').onclick = () => {
    const ids = state.evidence.filter(e => ['strategy','script','context','meta'].includes(e.evidence_kind)).map(e => e.evidence_id);
    state.selectedEvidenceIds = ids.length && ids.every(id => state.selectedEvidenceIds.has(id)) ? new Set() : new Set(ids);
    render();
  };
  document.getElementById('select-pending').onclick = () => { state.selectedEvidenceIds = new Set(state.evidence.filter(e => e.status !== 'approved' && ['strategy','script','context','meta'].includes(e.evidence_kind)).map(e => e.evidence_id)); render(); };
  document.getElementById('extract-all-strategy').onclick = event => runButton(event.currentTarget, '整份访谈提炼中...', async () => {
    const result = await post('/llm-extract-strategy', { source_id: currentSourceId(), include_all: true });
    if (result.error) return log(`Graph 提炼失败：${errorText(result)}`, 'error');
    await refreshBase();
    state.selectedKnowledge = state.knowledge.find(item => item.object_id === result.graph_id) || null;
    state.view = 'knowledge';
    log(`完整访谈已提炼：${result.effective_max_utts} 条证据，模型 ${result.llm_model || state.config.model}`, 'ok');
    switchView('knowledge');
  });
  document.getElementById('approve-g2').onclick = event => runButton(event.currentTarget, '提交 G2...', async () => {
    const refs = [...state.selectedEvidenceIds];
    if (!refs.length) return log('请先勾选要通过 G2 的证据', 'error');
    const result = await post('/gate', { gate_id: 'G2', task_id: currentTaskId(), reviewer: 'admin', decision: 'approved', reason: '人工完成证据归类复核', evidence_refs: refs });
    if (result.error) return log(`G2 阻断：${errorText(result)}`, 'error');
    state.selectedEvidenceIds.clear();
    await reloadAndRender(`G2 已通过 ${refs.length} 条证据`);
  });
  const reviewButton = document.getElementById('review-evidence');
  if (reviewButton) reviewButton.onclick = () => runButton(reviewButton, '保存中...', async () => {
    const result = await post('/evidence/review', { task_id: currentTaskId(), evidence_id: state.selectedEvidence.evidence_id, reviewer: document.getElementById('evidence-reviewer').value.trim(), decision: document.getElementById('evidence-decision').value, evidence_kind: document.getElementById('evidence-kind').value, conflict_set: document.getElementById('evidence-conflict').value.trim() || null, reason: document.getElementById('evidence-reason').value.trim() });
    if (result.error) return log(`证据审核失败：${errorText(result)}`, 'error');
    await reloadAndRender(`证据审核已保存：${result.audit_id}`);
  });
}

async function loadGraphLayout(force = false) {
  const graphId = state.graphCandidateId;
  if (!graphId) return;
  const query = new URLSearchParams({ task_id: currentTaskId(), graph_id: graphId });
  const current = await api(`/graph-layout?${query}`);
  if (current.error) return log(`流程布局读取失败：${errorText(current)}`, 'error');
  if (state.graphCandidateId !== graphId) return;
  state.graphLayoutProfile = current;
  renderGraphCanvas();
  // 布局分析可能调用外部模型；读取页面只消费已有缓存，由用户明确点击按钮才重跑。
  if (!force) return;
  const analyzed = await post('/graph-layout/analyze', {
    task_id: currentTaskId(), graph_id: graphId,
    reviewer: document.getElementById('graph-reviewer')?.value.trim() || 'system',
  });
  if (state.graphCandidateId !== graphId) return;
  if (analyzed.error) {
    state.graphLayoutProfile = analyzed.layout || { ...current, status: 'failed', last_error: analyzed };
    log(`电话流程布局分析失败，已保留拓扑分层：${errorText(analyzed)}`, 'error');
  } else {
    state.graphLayoutProfile = analyzed;
    state.graphLayout = 'call_flow';
    log(analyzed.deduplicated ? '已复用电话流程布局分析' : '电话流程七阶段布局已生成并缓存', 'ok');
  }
  if (state.view === 'knowledge') render();
}

function bindKnowledge() {
  document.getElementById('knowledge-refresh').onclick = () => reloadAndRender('Graph 与证据回链已刷新');
  const candidate = document.getElementById('graph-candidate');
  if (candidate) candidate.onchange = event => {
    state.graphCandidateId = event.target.value;
    state.graphLayoutProfile = null;
    const graph = state.knowledge.find(item => item.object_id === state.graphCandidateId);
    state.selectedKnowledge = graph || null;
    if (graph?.linkage?.baseline_id) state.graphBaselineId = graph.linkage.baseline_id;
    render();
  };
  const mode = document.getElementById('graph-mode'); if (mode) mode.onchange = event => { state.graphMode = event.target.value; renderGraphCanvas(); };
  const baseline = document.getElementById('graph-baseline'); if (baseline) baseline.onchange = event => { state.graphBaselineId = event.target.value; renderGraphCanvas(); };
  const layout = document.getElementById('graph-layout-select'); if (layout) layout.onchange = event => { state.graphLayout = event.target.value; renderGraphCanvas(); if (state.graphLayout === 'call_flow') loadGraphLayout(); };
  const analyzeLayout = document.getElementById('graph-layout-analyze');
  if (analyzeLayout) analyzeLayout.onclick = event => runButton(event.currentTarget, '分析中...', () => loadGraphLayout(true));
  const resetLayout = document.getElementById('graph-layout-reset');
  if (resetLayout) resetLayout.onclick = event => runButton(event.currentTarget, '初始化中...', async () => {
    if (!state.graphLayoutProfile) return log('布局仍在加载，请稍后再试', 'error');
    if (!window.confirm('将清空所有拖动保存的节点坐标，并按当前阶段和分支方向重新排版。继续吗？')) return;
    const result = await post('/graph-layout', {
      task_id: currentTaskId(), graph_id: state.graphCandidateId,
      materialized_graph_hash: state.graphLayoutProfile.materialized_graph_hash,
      reset_positions: true,
      editor: document.getElementById('graph-reviewer')?.value.trim() || 'admin',
    });
    if (result.error) return log(`布局初始化失败：${errorText(result)}`, 'error');
    state.graphLayoutProfile = result;
    state.graphLayout = 'call_flow';
    renderGraphCanvas();
    log('已清空人工坐标并重新初始化布局；未调用 LLM', 'ok');
  });
  const exportButton = document.getElementById('graph-export');
  if (exportButton) exportButton.onclick = event => runButton(event.currentTarget, '导出中...', async () => {
    if (!state.graphCandidateId) return log('当前没有可导出的 Graph', 'error');
    const query = new URLSearchParams({ task_id: currentTaskId(), graph_id: state.graphCandidateId });
    const result = await api(`/graph-export?${query}`);
    if (result.error) return log(`Graph 导出失败：${errorText(result)}`, 'error');
    const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob); link.download = result.filename || `graph_${state.graphCandidateId}.json`; link.click(); URL.revokeObjectURL(link.href);
    log(`Graph JSON 已导出：${result.content_hash?.slice(0, 12) || '--'}`, 'ok');
  });
  renderGraphCanvas();
  loadGraphLayout();
  document.querySelectorAll('.graph-condition-issue-edge').forEach(button => {
    button.onclick = () => {
      const edgeId = button.dataset.conditionEdgeId;
      const cy = window.__activeGraph;
      const edge = cy?.getElementById(edgeId);
      if (edge?.length) {
        edge.select();
        edge.emit('tap');
        cy.animate({ center: { eles: edge }, duration: 220 });
      } else {
        const currentModel = graphViewModel();
        const displayed = graphElements(currentModel);
        graphDetail(displayed.edges.find(item => item.id === edgeId), currentModel);
      }
    };
  });
  const review = async decision => {
    if (!state.graphCandidateId) return log('当前没有可审核的候选 Graph', 'error');
    const result = await post('/graph-review', {
      task_id: currentTaskId(), graph_id: state.graphCandidateId,
      reviewer: document.getElementById('graph-reviewer').value.trim(), decision,
      reason: document.getElementById('graph-review-reason').value.trim()
    });
    if (result.error) return log(`整图审核失败：${errorText(result)}`, 'error');
    await reloadAndRender(`${decision === 'approved' ? '整张候选图已批准' : '整张候选图已驳回'}：同步处理 ${result.evidence_refs?.length || 0} 条后台证据，下一 Gate ${result.next_gate || '--'}`);
  };
  const approve = document.getElementById('approve-graph'); if (approve) approve.onclick = event => runButton(event.currentTarget, '整图批准中...', () => review('approved'));
  const reject = document.getElementById('reject-graph'); if (reject) reject.onclick = event => runButton(event.currentTarget, '整图驳回中...', () => review('rejected'));
}

async function loadReleaseData() {
  const taskId = encodeURIComponent(currentTaskId());
  const [compilations, releases] = await Promise.all([api(`/compilations?task_id=${taskId}`), api(`/releases?task_id=${taskId}`)]);
  state.compilations = compilations.compilations || [];
  state.releases = releases.releases || [];
  state.selectedCompilation = state.selectedCompilation?.manifest?.task_id === currentTaskId() ? state.selectedCompilation : null;
  state.selectedRelease = state.selectedRelease?.release_manifest?.task_id === currentTaskId() ? state.selectedRelease : null;
  if (state.compilations.length) {
    const id = state.selectedCompilation?.compile_id || state.compilations.at(-1).compile_id;
    state.selectedCompilation = await api(`/compilation/${id}`);
  }
  if (state.releases.length) {
    const id = state.selectedRelease?.release_id || state.releases.at(-1).release_id;
    state.selectedRelease = await api(`/release/${id}`);
  }
}
function bindRelease() {
  const refresh = document.getElementById('release-refresh'); refresh.onclick = async () => { await loadReleaseData(); render(); log('编译与发布数据已刷新', 'ok'); };
  [['generate-execution','/generate-execution-prompt','电话执行 Prompt'],['generate-strategy','/llm-generate-strategy-prompt','策略评价 Prompt'],['generate-script','/llm-generate-script-prompt','话术评价 Prompt']].forEach(([id,path,label]) => {
    document.getElementById(id).onclick = event => runButton(event.currentTarget, '生成中...', async () => {
      const result = await post(path, { task_id: currentTaskId(), source_id: currentSourceId(), scope: 'general' });
      log(result.error ? `${label}失败：${errorText(result)}` : `${label}已生成，输入 ${result.input_count} 个对象`, result.error ? 'error' : 'ok');
    });
  });
  document.getElementById('compile-prompts').onclick = event => runButton(event.currentTarget, '编译中...', async () => {
    const result = await post('/compile', { task_id: currentTaskId(), source_id: currentSourceId(), scope: 'general' });
    if (result.error) return log(`编译阻断：${errorText(result)}`, 'error');
    state.selectedCompilation = await api(`/compilation/${result.compile_id}`);
    state.compilations.push(result);
    render(); log(`编译完成：${result.compile_id}`, 'ok');
  });
  [['approve-g4','G4'],['approve-g5','G5']].forEach(([id,gateId]) => {
    document.getElementById(id).onclick = event => runButton(event.currentTarget, '提交中...', async () => {
      const result = await post('/gate', { gate_id: gateId, task_id: currentTaskId(), reviewer: document.getElementById('release-owner').value.trim(), decision: 'approved', reason: document.getElementById('release-reason').value.trim() });
      if (result.error) return log(`${gateId} 阻断：${errorText(result)}`, 'error');
      await reloadAndRender(`${gateId} 已通过：${result.audit_id}`);
    });
  });
  const create = document.getElementById('create-release'); if (create) create.onclick = () => runButton(create, '发布中...', async () => {
    const result = await post('/release', { compile_id: state.selectedCompilation.compile_id, release_owner: document.getElementById('release-owner').value.trim(), scope: 'general' });
    if (result.error) return log(`发布阻断：${errorText(result)}`, 'error');
    state.selectedRelease = result; state.releases.push(result); render(); log(`发布完成：${result.release_id}`, 'ok');
  });
  const exportButton = document.getElementById('export-release'); if (exportButton) exportButton.onclick = () => {
    const blob = new Blob([JSON.stringify(state.selectedRelease, null, 2)], { type: 'application/json;charset=utf-8' });
    const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = `${state.selectedRelease.release_id}.json`; link.click(); URL.revokeObjectURL(link.href); log('发布包 JSON 已导出', 'ok');
  };
  const delivery = document.getElementById('record-delivery'); if (delivery) delivery.onclick = () => runButton(delivery, '记录中...', async () => {
    const result = await post('/delivery', { release_id: state.selectedRelease.release_id, deliverer: 'admin', recipient: 'manual-recipient', method: 'manual_copy', integrity_verified: true, note: '前端人工完整性交付' });
    log(result.error ? `交付记录失败：${errorText(result)}` : `交付已记录：${result.delivery_id}`, result.error ? 'error' : 'ok');
  });
}

async function loadAuditEvents() {
  const result = await api('/audit');
  const node = document.getElementById('audit-events');
  const events = (result.events || []).slice().reverse();
  node.innerHTML = `<div class="panel-title"><h3>不可变审计事件</h3>${badge(`${events.length} 条`, 'blue')}</div><div class="table-wrap"><table class="data-table"><thead><tr><th>时间 / ID</th><th>动作</th><th>结果</th></tr></thead><tbody>${events.map(e => `<tr><td>${esc(e.timestamp)}<br><span class="mono muted">${esc(e.audit_id)}</span></td><td>${esc(e.action)}<br><span class="muted">${esc(e.actor)}</span></td><td>${badge(e.result, e.result === 'approved' || e.result === 'saved' ? 'green' : '')}</td></tr>`).join('')}</tbody></table></div>`;
}
function bindAudit() {
  loadAuditEvents();
  document.getElementById('audit-refresh').onclick = () => loadAuditEvents();
  document.getElementById('save-config').onclick = event => runButton(event.currentTarget, '保存中...', async () => {
    const result = await post('/llm-config', { base_url: document.getElementById('cfg-url').value.trim(), model: document.getElementById('cfg-model').value.trim(), temperature: Number(document.getElementById('cfg-temp').value), max_tokens: Number(document.getElementById('cfg-tokens').value), max_utterances_per_call: Number(document.getElementById('cfg-utts').value), api_key: document.getElementById('cfg-key').value.trim() || undefined });
    if (result.error) return log(`配置保存失败：${errorText(result)}`, 'error');
    state.config = result; updateContext(); log(`配置已保存：${result.audit_id}`, 'ok');
  });
  document.getElementById('test-config').onclick = event => runButton(event.currentTarget, '测试中...', async () => {
    const result = await api('/llm-config/test');
    log(result.error ? `LLM 连接失败：${errorText(result)}` : `LLM 正常：${result.model}`, result.error ? 'error' : 'ok');
  });
}

document.querySelectorAll('.nav-item').forEach(item => item.onclick = () => switchView(item.dataset.view));
document.getElementById('task-switcher').onchange = async event => {
  state.currentTask = state.tasks.find(task => task.task_id === event.target.value) || null;
  state.selectedEvidence = null; state.selectedKnowledge = null; state.selectedEvidenceIds.clear();
  state.selectedCompilation = null; state.selectedRelease = null; state.compilations = []; state.releases = [];
  state.selectedGraphItem = null; state.graphBaselineId = state.currentTask?.baseline_id || null; state.graphCandidateId = null; state.graphLayoutProfile = null;
  await refreshBase(); render();
};

async function init() {
  await refreshBase();
  await loadReleaseData();
  render();
  log('前端已连接后端，业务状态加载完成', 'ok');
}
init();
