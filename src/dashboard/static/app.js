const state = {
  overview: null,
  guilds: [],
  tools: [],
  selectedGuildId: '',
  currentSection: 'overview',
  toolQuery: '',
};

const titles = {
  overview: '總覽',
  agents: 'Agents',
  queue: 'Queue',
  guilds: 'Guilds',
  logs: 'Logs',
  memory: 'Memory',
  tools: 'Tools',
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function text(selector, value) {
  const element = $(selector);
  if (element) element.textContent = value;
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

function formatDate(value) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('zh-TW', { hour12: false });
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

async function api(path) {
  const response = await fetch(path, { cache: 'no-store' });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

function setConnection(ok, message) {
  const dot = $('#connection-dot');
  const label = $('#connection-label');
  dot.classList.toggle('ok', ok);
  dot.classList.toggle('fail', !ok);
  label.textContent = message;
}

function badge(label, variant = '') {
  return `<span class="badge ${variant}">${escapeHtml(label)}</span>`;
}

function renderOverview() {
  const overview = state.overview;
  if (!overview) return;

  const agents = overview.agents || [];
  const online = agents.filter((agent) => agent.is_online).length;
  const queue = overview.queue || {};
  const pending = (queue.priority_queue_size || 0) + (queue.normal_queue_size || 0) + (queue.retry_queue_size || 0);

  text('#metric-agents', agents.length);
  text('#metric-online', `${online} online`);
  text('#metric-queue', pending);
  text('#metric-tools', overview.tools_count || 0);
  text('#metric-uptime', formatDuration(overview.uptime_seconds));
  text('#runtime-council', `${overview.council?.enabled ? overview.council.current_state : 'disabled'}`);
  text('#runtime-override', `${overview.override?.enabled ? `${overview.override.pending_count} pending` : 'disabled'}`);
  text('#runtime-processed', queue.total_processed || 0);
  text('#runtime-failed', queue.total_failed || 0);
  text('#agent-summary', `${agents.length} entries`);

  const list = $('#agent-list');
  list.innerHTML = agents.map((agent) => `
    <div class="agent-row">
      <div class="agent-name">${escapeHtml(agent.name)}</div>
      <div class="agent-meta">${escapeHtml(agent.personality || '-')}</div>
      ${badge(agent.is_online ? 'online' : 'offline', agent.is_online ? '' : 'offline')}
    </div>
  `).join('') || '<div class="empty-hint">沒有 Agent。</div>';
}

function renderAgents() {
  const agents = state.overview?.agents || [];
  $('#agents-table').innerHTML = agents.map((agent) => {
    const context = `${agent.context_tokens ?? 0} / ${agent.token_budget ?? 0}`;
    return `
      <tr>
        <td><strong>${escapeHtml(agent.name)}</strong></td>
        <td>${badge(agent.is_online ? 'online' : 'offline', agent.is_online ? '' : 'offline')}</td>
        <td class="truncate">${escapeHtml(agent.personality || '-')}</td>
        <td class="code-cell">${escapeHtml(context)}</td>
        <td>${escapeHtml(agent.pending_actions ?? 0)}</td>
      </tr>
    `;
  }).join('') || '<tr><td colspan="5">沒有 Agent。</td></tr>';
}

async function renderQueue() {
  const queue = state.overview?.queue || await api('/api/queue');
  text('#queue-priority', queue.priority_queue_size || 0);
  text('#queue-normal', queue.normal_queue_size || 0);
  text('#queue-retry', queue.retry_queue_size || 0);
  text('#queue-failed', queue.total_failed || 0);

  const status = $('#task-status-filter').value;
  const path = status === 'all' ? '/api/tasks?limit=50' : `/api/tasks?status=${encodeURIComponent(status)}&limit=50`;
  const tasks = await api(path);
  $('#tasks-table').innerHTML = tasks.map((task) => `
    <tr>
      <td class="code-cell">${escapeHtml(task.id)}</td>
      <td class="code-cell">${escapeHtml(task.guild_id || '-')}</td>
      <td>${escapeHtml(task.agent_name || '-')}</td>
      <td>${escapeHtml(task.task_type || '-')}</td>
      <td>${badge(task.status || '-', task.status === 'failed' ? 'offline' : task.status === 'retry' ? 'warn' : '')}</td>
      <td>${escapeHtml(formatDate(task.updated_at || task.created_at))}</td>
    </tr>
  `).join('') || '<tr><td colspan="6">沒有任務。</td></tr>';
}

function renderGuildSelector() {
  const select = $('#guild-select');
  const current = state.selectedGuildId;
  select.innerHTML = '<option value="">選擇 Guild</option>' + state.guilds.map((guild) => `
    <option value="${escapeHtml(guild.id)}">${escapeHtml(guild.name)}</option>
  `).join('');
  select.value = current;
}

function renderGuilds() {
  const list = $('#guild-list');
  list.innerHTML = state.guilds.map((guild) => `
    <article class="guild-card">
      <div>
        <h3>${escapeHtml(guild.name)}</h3>
        <p class="code-cell">${escapeHtml(guild.id)}</p>
      </div>
      <div class="guild-stats">
        <div><strong>${escapeHtml(guild.member_count)}</strong><span>members</span></div>
        <div><strong>${escapeHtml(guild.text_channels)}</strong><span>text</span></div>
        <div><strong>${escapeHtml(guild.voice_channels)}</strong><span>voice</span></div>
      </div>
      <div class="muted-text">${escapeHtml((guild.connected_agents || []).join(', ') || 'no agents')}</div>
    </article>
  `).join('') || '<div class="empty-hint">Bot 尚未連上任何 Guild，或目前 token 是 placeholder。</div>';
}

async function renderLogs() {
  const hasGuild = Boolean(state.selectedGuildId);
  $('#logs-hint').classList.toggle('hidden', hasGuild);
  $('#logs-wrap').classList.toggle('hidden', !hasGuild);
  if (!hasGuild) return;

  const [actionRows, toolRows] = await Promise.all([
    api(`/api/action-logs?guild_id=${encodeURIComponent(state.selectedGuildId)}&limit=80`),
    api(`/api/tool-calls?guild_id=${encodeURIComponent(state.selectedGuildId)}&limit=80`),
  ]);

  const rows = [
    ...actionRows.map((row) => ({
      kind: 'action',
      timestamp: row.created_at,
      agent_name: row.agent_name,
      tool_name: row.tool_name || row.action,
      status: row.status,
      reason: row.reason,
    })),
    ...toolRows.map((row) => ({
      kind: 'tool',
      timestamp: row.executed_at || row.created_at,
      agent_name: row.agent_name,
      tool_name: row.tool_name,
      status: row.actual_result ? 'executed' : 'pending',
      reason: row.reasoning || row.expected_result || '-',
    })),
  ].sort((left, right) => String(right.timestamp || '').localeCompare(String(left.timestamp || '')));

  $('#logs-table').innerHTML = rows.map((row) => `
    <tr>
      <td>${badge(row.kind, row.kind === 'tool' ? 'warn' : '')}</td>
      <td>${escapeHtml(formatDate(row.timestamp))}</td>
      <td>${escapeHtml(row.agent_name || '-')}</td>
      <td>${escapeHtml(row.tool_name || '-')}</td>
      <td>${badge(row.status || '-')}</td>
      <td class="truncate">${escapeHtml(row.reason || '-')}</td>
    </tr>
  `).join('') || '<tr><td colspan="6">沒有操作日誌。</td></tr>';
}

async function renderMemory() {
  const hasGuild = Boolean(state.selectedGuildId);
  $('#memory-hint').classList.toggle('hidden', hasGuild);
  $('#memory-wrap').classList.toggle('hidden', !hasGuild);
  if (!hasGuild) return;

  const rows = await api(`/api/memory?guild_id=${encodeURIComponent(state.selectedGuildId)}`);
  $('#memory-table').innerHTML = rows.map((row) => `
    <tr>
      <td>${escapeHtml(row.category || '-')}</td>
      <td class="code-cell">${escapeHtml(row.key || '-')}</td>
      <td class="truncate">${escapeHtml(row.value || '-')}</td>
      <td>${escapeHtml(row.confidence ?? '-')}</td>
      <td>${escapeHtml(formatDate(row.updated_at || row.created_at))}</td>
    </tr>
  `).join('') || '<tr><td colspan="5">沒有長期記憶。</td></tr>';
}

function renderTools() {
  const query = state.toolQuery.trim().toLowerCase();
  const tools = state.tools.filter((tool) => !query || tool.name.toLowerCase().includes(query));
  $('#tool-list').innerHTML = tools.map((tool) => `
    <div class="tool-chip">
      <span>${escapeHtml(tool.name)}</span>
      ${badge(tool.safety_level || 'UNKNOWN', tool.safety_level === 'DANGEROUS' ? 'offline' : tool.safety_level === 'ADMIN' ? 'warn' : '')}
    </div>
  `).join('') || '<div class="empty-hint">沒有符合的工具。</div>';
}

async function renderCurrentSection() {
  renderOverview();
  renderAgents();
  renderGuilds();
  renderTools();
  if (state.currentSection === 'queue') await renderQueue();
  if (state.currentSection === 'logs') await renderLogs();
  if (state.currentSection === 'memory') await renderMemory();
}

async function refreshAll() {
  try {
    const [overview, guilds, tools] = await Promise.all([
      api('/api/overview'),
      api('/api/guilds'),
      api('/api/tools'),
    ]);
    state.overview = overview;
    state.guilds = guilds;
    state.tools = tools;

    if (!state.selectedGuildId && guilds.length === 1) {
      state.selectedGuildId = guilds[0].id;
    }
    if (state.selectedGuildId && !guilds.some((guild) => guild.id === state.selectedGuildId)) {
      state.selectedGuildId = '';
    }

    renderGuildSelector();
    await renderCurrentSection();
    setConnection(true, '已連線');
  } catch (error) {
    setConnection(false, `離線：${error.message}`);
  }
}

function switchSection(section) {
  state.currentSection = section;
  $$('.nav-item').forEach((button) => button.classList.toggle('active', button.dataset.section === section));
  $$('.section').forEach((panel) => panel.classList.toggle('active', panel.id === `section-${section}`));
  text('#section-title', titles[section] || section);
  renderCurrentSection();
}

function setupEvents() {
  $$('.nav-item').forEach((button) => {
    button.addEventListener('click', () => switchSection(button.dataset.section));
  });

  $('#refresh-button').addEventListener('click', refreshAll);
  $('#guild-select').addEventListener('change', (event) => {
    state.selectedGuildId = event.target.value;
    renderCurrentSection();
  });
  $('#task-status-filter').addEventListener('change', renderQueue);
  $('#tool-search').addEventListener('input', (event) => {
    state.toolQuery = event.target.value;
    renderTools();
  });
}

setupEvents();
refreshAll();
setInterval(refreshAll, 10000);
