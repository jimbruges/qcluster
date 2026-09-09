'use strict';

const $ = (id) => document.getElementById(id);
const state = { engine: null, models: [], nodes: [], controller: null, history: [] };

const TOKEN_KEY = 'qcluster.token';
const getToken = () => localStorage.getItem(TOKEN_KEY) || '';
const setToken = (value) => value
  ? localStorage.setItem(TOKEN_KEY, value)
  : localStorage.removeItem(TOKEN_KEY);

function authHeaders(base = {}) {
  const token = getToken();
  return token ? { ...base, Authorization: `Bearer ${token}` } : { ...base };
}

function promptForToken(message = 'This QCluster is password protected.') {
  const value = prompt(`${message}\nEnter the access password:`);
  if (value === null) return false;
  setToken(value.trim());
  return true;
}

/* ---------- tabs ---------- */
document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach((p) => p.classList.remove('active'));
    tab.classList.add('active');
    $(`panel-${tab.dataset.panel}`).classList.add('active');
    if (tab.dataset.panel === 'settings') refreshSudo();
  });
});

/* ---------- helpers ---------- */
const mb = (v) => (v >= 1024 ? `${(v / 1024).toFixed(1)} GB` : `${Math.round(v)} MB`);

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: authHeaders({ 'Content-Type': 'application/json', ...(options.headers || {}) }),
  });
  if (res.status === 401) {
    if (promptForToken()) return api(path, options);
    throw new Error('access password required');
  }
  const text = await res.text();
  const body = text ? JSON.parse(text) : {};
  if (!res.ok) throw new Error(body.error?.message || body.error || res.statusText);
  return body;
}

/* ---------- live feed ---------- */
function connect() {
  const token = getToken();
  const url = token ? `/api/events?token=${encodeURIComponent(token)}` : '/api/events';
  const source = new EventSource(url);
  let gotData = false;
  source.onmessage = (event) => { gotData = true; render(JSON.parse(event.data)); };
  source.onerror = async () => {
    source.close();
    $('engine-dot').className = 'dot error';
    if (!gotData) {
      // Most likely a 401: ask for the password, then reconnect.
      try {
        const { token_set: needsToken } = await (await fetch('/api/auth')).json();
        if (needsToken && promptForToken()) return connect();
      } catch { /* daemon down */ }
    }
    $('engine-label').textContent = 'disconnected';
    setTimeout(connect, 3000);
  };
}

function render(data) {
  state.engine = data.engine;
  state.models = data.models || [];
  state.nodes = data.nodes || [];
  renderEngine(data);
  renderNodes(data);
  renderModels();
  renderApi(data);
  renderSettings(data);
}

function renderEngine(data) {
  const engine = data.engine;
  const dot = $('engine-dot');
  const label = $('engine-label');
  dot.className = `dot ${engine.state === 'ready' ? 'ready' : engine.state === 'loading' ? 'loading' : engine.state === 'error' ? 'error' : ''}`;
  if (engine.state === 'ready') label.textContent = `${engine.model_id} loaded`;
  else if (engine.state === 'loading') label.textContent = `loading ${engine.model_id}… ${engine.load_percent}%`;
  else if (engine.state === 'error') label.textContent = engine.error || 'engine error';
  else label.textContent = 'no model loaded';
}

function renderNodes(data) {
  $('cluster-summary').innerHTML =
    `<strong>${data.ready_count}</strong> of <strong>${data.board_count}</strong> boards ready · ` +
    `<strong>${mb(data.total_usable_mb)}</strong> of pooled RAM available for a model`;

  $('node-cards').innerHTML = state.nodes.map((node) => {
    const s = node.stats || {};
    const cpu = s.cpu_percent ?? 0;
    const ram = s.mem_percent ?? 0;
    const temp = s.temp_c != null ? `${s.temp_c} °C` : '—';
    const actions = node.role === 'host' ? '' : `
      <div class="card-actions">
        <button class="small" data-reprovision="${node.serial}">Reprovision</button>
        <button class="small" data-rpc="${node.serial}">Restart RPC</button>
      </div>`;
    return `
      <div class="card">
        <div class="card-head">
          <div>
            <div class="card-title">${node.role === 'host' ? 'Host board' : `Board ${node.slot}`}</div>
            <div class="card-sub">${node.serial}</div>
          </div>
          <span class="pill ${node.state}">${node.state}</span>
        </div>
        <div class="meter">
          <div class="meter-label"><span>CPU</span><span>${cpu.toFixed(0)}%</span></div>
          <div class="meter-track"><div class="meter-fill" style="width:${cpu}%"></div></div>
        </div>
        <div class="meter">
          <div class="meter-label"><span>RAM</span><span>${ram.toFixed(0)}% of ${mb(s.mem_total_mb || 0)}</span></div>
          <div class="meter-track"><div class="meter-fill ram" style="width:${ram}%"></div></div>
        </div>
        <div class="card-foot">
          <span>${temp}</span>
          <span>${s.cores || '?'} cores</span>
          <span>free for model: ${mb(node.usable_mb || 0)}</span>
          ${node.role === 'host' ? '' : `<span>rpc ${node.rpc_running ? 'up' : 'down'} :${node.rpc_host_port}</span>`}
        </div>
        ${node.error ? `<div class="card-foot" style="color:var(--danger)">${node.error}</div>` : ''}
        ${actions}
      </div>`;
  }).join('');
}

function fitLabel(model, data) {
  const pooled = state.nodes.filter((n) => n.state === 'ready').length;
  const total = state.nodes.filter((n) => n.state === 'ready')
    .reduce((sum, n) => sum + (n.usable_mb || 0), 0);
  const host = state.nodes.find((n) => n.role === 'host');
  if (host && model.ram_mb <= (host.usable_mb || 0)) {
    return '<span class="fit-ok">fits host alone</span>';
  }
  if (model.ram_mb <= total) {
    return `<span class="fit-pool">needs pooling (${pooled} boards)</span>`;
  }
  return '<span class="fit-no">will not fit</span>';
}

function renderModels() {
  const engine = state.engine || {};
  $('model-rows').innerHTML = state.models.map((model) => {
    const dl = model.download;
    const isActive = engine.model_id === model.id;
    let action;
    if (dl && dl.state === 'downloading') {
      action = `<button class="small danger" data-cancel="${model.id}">Cancel ${dl.percent}%</button>
                <div class="progress"><div style="width:${dl.percent}%"></div></div>`;
    } else if (!model.downloaded) {
      action = `<button class="small" data-download="${model.id}">Download</button>`;
    } else if (isActive) {
      action = `<button class="small danger" data-unload="1">Unload</button>`;
    } else {
      action = `<button class="small" data-load="${model.id}">Load</button>
                <button class="small danger" data-delete="${model.id}">${model.custom ? 'Remove' : 'Delete'}</button>`;
    }
    return `
      <tr>
        <td><strong>${model.name}</strong>${model.custom ? ' <span class="pill">custom</span>' : ''}<br><span class="card-sub">${model.notes}</span></td>
        <td>${model.params}</td>
        <td>${model.quant}</td>
        <td>${mb(model.file_size_mb)}</td>
        <td>${fitLabel(model)}</td>
        <td>${action}</td>
      </tr>`;
  }).join('');
}

function renderApi(data) {
  const base = `${location.origin}/v1`;
  $('api-base').textContent = base;
  const model = data.engine.alias || 'no-model-loaded';
  $('api-model').textContent = model;
  $('api-auth').textContent = data.auth_required
    ? 'Authorization: Bearer <QCLUSTER_TOKEN>'
    : 'none (set QCLUSTER_TOKEN to require a bearer token)';
  const auth = data.auth_required ? ` \\\n  -H "Authorization: Bearer $QCLUSTER_TOKEN"` : '';

  $('snippet-curl').textContent =
`curl ${base}/chat/completions \\
  -H "Content-Type: application/json"${auth} \\
  -d '{
    "model": "${model}",
    "messages": [{"role": "user", "content": "What is edge AI?"}],
    "max_tokens": 128,
    "stream": true
  }'`;

  $('snippet-python').textContent =
`from openai import OpenAI

client = OpenAI(base_url="${base}", api_key="${data.auth_required ? '<QCLUSTER_TOKEN>' : 'not-needed'}")

stream = client.chat.completions.create(
    model="${model}",
    messages=[{"role": "user", "content": "What is edge AI?"}],
    max_tokens=128,
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="", flush=True)`;

  $('snippet-requests').textContent =
`import json, requests

with requests.post(
    "${base}/chat/completions",
    json={
        "model": "${model}",
        "messages": [{"role": "user", "content": "What is edge AI?"}],
        "max_tokens": 128,
        "stream": True,
    },${data.auth_required ? '\n    headers={"Authorization": f"Bearer {TOKEN}"},' : ''}
    stream=True,
    timeout=600,
) as response:
    for line in response.iter_lines():
        if line.startswith(b"data: ") and line[6:] != b"[DONE]":
            delta = json.loads(line[6:])["choices"][0]["delta"]
            print(delta.get("content", ""), end="", flush=True)`;

  const metrics = data.engine.last_metrics || {};
  $('metrics').textContent = metrics.predicted_per_second
    ? `last response: ${metrics.predicted_per_second} tok/s generation, ` +
      `${metrics.prompt_per_second} tok/s prompt, ${metrics.predicted_tokens} tokens`
    : '';
}

function renderSettings(data) {
  const w = data.wifi || {};
  $('wifi-device').textContent = w.device || (w.available ? '—' : 'no WiFi interface');
  $('wifi-ssid').textContent = w.ssid || 'not connected';
  $('wifi-ip').textContent = w.ip || '—';
  $('wifi-signal').textContent = w.signal != null ? `${w.signal}%` : '—';

  $('board-admin').innerHTML = state.nodes.filter((n) => n.role !== 'host').map((node) => `
    <div class="card">
      <div class="card-head">
        <div>
          <div class="card-title">Board ${node.slot}</div>
          <div class="card-sub">${node.serial}</div>
        </div>
        <span class="pill ${node.state}">${node.state}</span>
      </div>
      <div class="card-foot"><span>${node.caps?.board || ''}</span></div>
      <div class="card-actions">
        ${node.state === 'decommissioned'
          ? `<button class="small" data-recommission="${node.serial}">Re-enable</button>`
          : `<button class="small danger" data-decommission="${node.serial}">Decommission</button>`}
      </div>
    </div>`).join('') || '<p class="hint">No child boards connected.</p>';
}

/* ---------- actions ---------- */
document.addEventListener('click', async (event) => {
  const target = event.target.closest('button');
  if (!target) return;
  const d = target.dataset;
  try {
    if (d.download) await api(`/api/models/${d.download}/download`, { method: 'POST' });
    else if (d.cancel) await api(`/api/models/${d.cancel}/cancel`, { method: 'POST' });
    else if (d.delete) await api(`/api/models/${d.delete}`, { method: 'DELETE' });
    else if (d.reprovision) await api(`/api/nodes/${d.reprovision}/reprovision`, { method: 'POST' });
    else if (d.rpc) await api(`/api/nodes/${d.rpc}/rpc/restart`, { method: 'POST' });
    else if (d.decommission) await decommission(d.decommission);
    else if (d.recommission) await api(`/api/nodes/${d.recommission}/recommission`, { method: 'POST' });
    else if (d.sudoSave) await sudoAction('save', d.sudoSave);
    else if (d.sudoForget) await sudoAction('forget', d.sudoForget);
    else if (d.sudoSet) await sudoAction('set', d.sudoSet);
    else if (d.unload || target.id === 'engine-stop') await api('/api/engine/stop', { method: 'POST' });
    else if (d.load) await loadModel(d.load);
    else if (target.id === 'rescan') await api('/api/nodes/rescan', { method: 'POST' });
  } catch (err) {
    alert(err.message);
  }
});

async function decommission(serial) {
  const removeApp = confirm(
    `Decommission board ${serial}?\n\n` +
    'This deletes ~/qcluster from the board and stops managing it.\n\n' +
    'OK = also remove the QCluster Display app (full clean-up)\n' +
    'Cancel = keep the display app installed'
  );
  const result = await api(`/api/nodes/${serial}/decommission`, {
    method: 'POST',
    body: JSON.stringify({ remove_app: removeApp }),
  });
  alert(result.message || 'Board cleaned.');
}

/* ---------- wifi ---------- */
$('wifi-scan').addEventListener('click', async () => {
  $('wifi-status').textContent = 'scanning…';
  try {
    const { networks } = await api('/api/wifi/scan');
    $('wifi-networks').innerHTML = networks
      .map((n) => `<option value="${n.ssid}">${n.signal}% ${n.security || 'open'}</option>`)
      .join('');
    $('wifi-status').textContent = `${networks.length} networks found`;
  } catch (err) {
    $('wifi-status').textContent = err.message;
  }
});

$('wifi-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const ssid = $('wifi-input-ssid').value.trim();
  $('wifi-status').textContent = `connecting to ${ssid}…`;
  try {
    const result = await api('/api/wifi/connect', {
      method: 'POST',
      body: JSON.stringify({ ssid, password: $('wifi-password').value }),
    });
    $('wifi-password').value = '';
    $('wifi-status').textContent = result.message || 'connected';
  } catch (err) {
    $('wifi-status').textContent = err.message;
  }
});

/* ---------- board shell (sudo) credentials ---------- */
const SUDO_LABELS = {
  'passwordless': ['ready', 'sudo works without a password'],
  'stored': ['ready', 'saved password verified'],
  'password-required': ['error', 'needs a password'],
  'stored-invalid': ['error', 'saved password rejected'],
  'unknown': ['lost', 'unreachable'],
};

async function refreshSudo() {
  $('sudo-status').textContent = 'checking boards…';
  try {
    const { boards } = await api('/api/sudo');
    $('sudo-boards').innerHTML = boards.map((b) => {
      const [pill, text] = SUDO_LABELS[b.state] || ['', b.state];
      const name = b.serial === 'host' ? 'Host board' : `Board ${b.serial}`;
      const canSet = b.state === 'passwordless' || b.state === 'stored';
      return `
        <div class="card">
          <div class="card-head">
            <div><div class="card-title">${name}</div>
                 <div class="card-sub">${b.serial}</div></div>
            <span class="pill ${pill}">${text}</span>
          </div>
          <div class="card-actions" style="flex-direction:column;align-items:stretch;gap:8px">
            <input type="password" placeholder="current sudo password"
                   data-sudo-input="${b.serial}" autocomplete="off">
            <div style="display:flex;gap:6px;flex-wrap:wrap">
              <button class="small" data-sudo-save="${b.serial}">Save &amp; verify</button>
              ${b.stored ? `<button class="small danger" data-sudo-forget="${b.serial}">Forget</button>` : ''}
            </div>
            ${canSet ? `
            <input type="password" placeholder="new password (min 8 chars)"
                   data-sudo-new="${b.serial}" autocomplete="new-password">
            <button class="small" data-sudo-set="${b.serial}">Set board password</button>` : ''}
          </div>
        </div>`;
    }).join('');
    $('sudo-status').textContent = '';
  } catch (err) {
    $('sudo-status').textContent = err.message;
  }
}

$('sudo-refresh').addEventListener('click', refreshSudo);

$('usb-rules').addEventListener('click', async () => {
  $('sudo-status').textContent = 'installing udev rules on the host…';
  try {
    const result = await api('/api/sudo/install-usb-rules', { method: 'POST' });
    $('sudo-status').textContent = result.message || 'done';
  } catch (err) {
    $('sudo-status').textContent = err.message;
  }
});

async function sudoAction(kind, serial) {
  const field = kind === 'set'
    ? document.querySelector(`[data-sudo-new="${serial}"]`)
    : document.querySelector(`[data-sudo-input="${serial}"]`);
  const password = field ? field.value : '';
  $('sudo-status').textContent = 'working…';
  try {
    let result;
    if (kind === 'save') {
      result = await api('/api/sudo/save', {
        method: 'POST', body: JSON.stringify({ serial, password }),
      });
    } else if (kind === 'forget') {
      result = await api('/api/sudo/forget', {
        method: 'POST', body: JSON.stringify({ serial }),
      });
    } else {
      result = await api('/api/sudo/set-password', {
        method: 'POST', body: JSON.stringify({ serial, password }),
      });
    }
    if (field) field.value = '';
    $('sudo-status').textContent = result.message || 'done';
    await refreshSudo();
  } catch (err) {
    $('sudo-status').textContent = err.message;
  }
}

/* ---------- custom models ---------- */
$('custom-model-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('custom-status').textContent = 'checking link…';
  try {
    const model = await api('/api/models/custom', {
      method: 'POST',
      body: JSON.stringify({ url: $('custom-url').value, name: $('custom-name').value }),
    });
    $('custom-url').value = '';
    $('custom-name').value = '';
    $('custom-status').textContent =
      `Added ${model.name} (${model.file_size_mb} MB). Download it from the table above.`;
  } catch (err) {
    $('custom-status').textContent = err.message;
  }
});

async function loadModel(modelId, force = false) {
  const payload = {
    model_id: modelId,
    ctx_size: Number($('ctx-size').value),
    threads: Number($('threads').value),
    force,
  };
  try {
    await api('/api/engine/start', { method: 'POST', body: JSON.stringify(payload) });
  } catch (err) {
    if (!force && err.message.includes('does not fit')) {
      if (confirm('This model exceeds the pooled RAM of the cluster. Try anyway (it may swap or be OOM-killed)?')) {
        return loadModel(modelId, true);
      }
      return;
    }
    throw err;
  }
}

/* ---------- engine log ---------- */
setInterval(async () => {
  if (!$('panel-cluster').classList.contains('active')) return;
  try {
    const { lines } = await api('/api/engine/logs');
    const box = $('engine-log');
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
    box.textContent = lines.length ? lines.slice(-120).join('\n') : 'no engine running';
    if (atBottom) box.scrollTop = box.scrollHeight;
  } catch { /* transient */ }
}, 2000);

/* ---------- chat ---------- */
function addMessage(role, text) {
  const node = document.createElement('div');
  node.className = `msg ${role}`;
  node.textContent = text;
  $('messages').appendChild(node);
  $('messages').scrollTop = $('messages').scrollHeight;
  return node;
}

$('prompt').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    $('chat-form').requestSubmit();
  }
});

$('stop').addEventListener('click', () => state.controller?.abort());

$('clear-chat').addEventListener('click', () => {
  state.controller?.abort();
  state.history = [];
  $('messages').innerHTML = '';
  $('metrics').textContent = '';
  updateContextNote();
});

function updateContextNote() {
  const turns = state.history.length;
  $('context-note').textContent = turns
    ? `${turns} message${turns === 1 ? '' : 's'} in context (last 8 are sent with each request)`
    : 'Context is empty.';
}

$('chat-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const prompt = $('prompt').value.trim();
  if (!prompt) return;
  if (!state.engine || state.engine.state !== 'ready') {
    addMessage('error', 'No model is loaded. Load one from the Models tab first.');
    return;
  }

  $('prompt').value = '';
  addMessage('user', prompt);
  state.history.push({ role: 'user', content: prompt });
  const bubble = addMessage('assistant', '');
  const started = performance.now();
  let firstToken = null;
  let answer = '';

  $('send').disabled = true;
  $('stop').hidden = false;
  state.controller = new AbortController();

  try {
    const response = await fetch('/v1/chat/completions', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      signal: state.controller.signal,
      body: JSON.stringify({
        model: state.engine.alias,
        messages: [
          { role: 'system', content: $('system-prompt').value },
          ...state.history.slice(-8),
        ],
        max_tokens: Number($('max-tokens').value),
        temperature: Number($('temperature').value),
        presence_penalty: 1.1,
        stream: true,
      }),
    });
    if (!response.ok) throw new Error((await response.text()).slice(0, 300));

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const payload = line.slice(6).trim();
        if (payload === '[DONE]') continue;
        try {
          const delta = JSON.parse(payload).choices?.[0]?.delta?.content;
          if (delta) {
            if (firstToken === null) firstToken = performance.now() - started;
            answer += delta;
            bubble.textContent = answer;
            $('messages').scrollTop = $('messages').scrollHeight;
          }
        } catch { /* keepalive or partial frame */ }
      }
    }
    state.history.push({ role: 'assistant', content: answer });
    const elapsed = (performance.now() - started) / 1000;
    $('metrics').textContent =
      `${elapsed.toFixed(1)} s total · first token ${(firstToken / 1000 || 0).toFixed(1)} s`;  } catch (err) {
    if (err.name !== 'AbortError') bubble.className = 'msg error', bubble.textContent = err.message;
  } finally {
    $('send').disabled = false;
    $('stop').hidden = true;
    state.controller = null;
    updateContextNote();
  }
});

updateContextNote();
connect();
