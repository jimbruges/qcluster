'use strict';

const $ = (id) => document.getElementById(id);
const state = {
  engine: null,
  models: [],
  nodes: [],
  controller: null,
  history: [],
  identifying: {},
  lastData: null,
};

const TOKEN_KEY = 'qcluster.token';
const getToken = () => localStorage.getItem(TOKEN_KEY) || '';

const STATE_LABELS = {
  discovered: 'not provisioned',
  provisioning: 'provisioning…',
  ready: 'ready',
  error: 'error',
  lost: 'lost',
  decommissioned: 'decommissioned',
};
const STATE_HINTS = {
  discovered: 'Detected but not yet provisioned — no runtime pushed, RPC server not running. It cannot take model layers yet.',
  provisioning: 'Runtime and RPC server are being set up on this board.',
  error: 'Provisioning failed. See the error below, or try Reprovision.',
  lost: 'The board is no longer visible over ADB.',
  decommissioned: 'Removed from cluster management. Recommission to bring it back.',
};
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
  state.lastData = data;
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
  tuneChatDefaults(engine.model_id);
}

function tuneChatDefaults(modelId) {
  const maxTokens = $('max-tokens');
  if (!maxTokens || maxTokens.dataset.userEdited) return;
  maxTokens.value = String((modelId || '').toLowerCase().includes('7b') ? 32 : 256);
}

function renderNodes(data) {
  const reclaimable = data.total_reclaimable_mb || 0;
  const freed = reclaimable
    ? ` &middot; <strong>${mb(data.total_usable_mb + reclaimable)}</strong> after unloading the current model`
    : '';
  $('cluster-summary').innerHTML =
    `<strong>${data.ready_count}</strong> of <strong>${data.board_count}</strong> boards ready &middot; ` +
    `<strong>${mb(data.total_usable_mb)}</strong> of pooled RAM free${freed}`;

  $('node-cards').innerHTML = state.nodes.map((node) => {
    const s = node.stats || {};
    const cpu = s.cpu_percent ?? 0;
    const ram = s.mem_percent ?? 0;
    const temp = s.temp_c != null ? `${s.temp_c} °C` : '—';
    const stateLabel = STATE_LABELS[node.state] || node.state;
    const stateTitle = STATE_HINTS[node.state] || '';
    const actions = `
      <div class="card-actions">
        ${identifyButton(node)}
        ${node.role === 'host' ? '' : `
          <button class="small" data-reprovision="${node.serial}">Reprovision</button>
          <button class="small" data-rpc="${node.serial}">Restart RPC</button>`}
      </div>`;
    return `
      <div class="card">
        <div class="card-head">
          <div>
            <div class="card-title">${node.role === 'host' ? 'Host board' : `Board ${node.slot}`}</div>
            <div class="card-sub">${node.serial}</div>
          </div>
          <span class="pill ${node.state}" title="${stateTitle}">${stateLabel}</span>
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
        ${(node.state === 'discovered' || node.state === 'provisioning') && node.role !== 'host'
          ? `<div class="card-foot" style="color:var(--warn)">${stateTitle}</div>` : ''}
        ${actions}
      </div>`;
  }).join('');
}

function fitLabel(model) {
  const fit = model.fit;
  if (!fit) return '<span class="card-sub">—</span>';
  const after = fit.reclaimed ? ' after unloading the current model' : '';
  const excludedNote = fit.excluded_boards
    ? ` ${fit.excluded_boards} lowest-capacity board(s) excluded: ggml's backend `
      + `scheduler caps total backends at 16 (15 RPC + 1 CPU).`
    : '';
  const detail = `needs ~${mb(fit.needed_mb)}, ${mb(fit.pooled_mb)} free across `
    + `${fit.boards} board${fit.boards === 1 ? '' : 's'}${after}.${excludedNote}`;
  if (fit.loaded) {
    return `<span class="fit-ok" title="${detail}">loaded now</span>`;
  }
  const note = fit.reclaimed ? ' <span class="card-sub">(after unload)</span>' : '';
  const excludedBadge = fit.excluded_boards
    ? ` <span class="card-sub">(${fit.excluded_boards} excluded)</span>` : '';
  if (fit.fits_host_alone) {
    return `<span class="fit-ok" title="${detail}">fits host alone</span>${note}`;
  }
  if (fit.fits) {
    return `<span class="fit-pool" title="${detail}">needs pooling (${fit.boards} boards)</span>${note}${excludedBadge}`;
  }
  return `<span class="fit-no" title="${detail}">needs ${mb(fit.needed_mb)}, ${mb(fit.pooled_mb)} free</span>`;
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
        ${identifyButton(node)}
        ${node.state === 'decommissioned'
          ? `<button class="small" data-recommission="${node.serial}">Re-enable</button>`
          : `<button class="small danger" data-decommission="${node.serial}">Decommission</button>`}
      </div>
    </div>`).join('') || '<p class="hint">No child boards connected.</p>';
}

function identifyButton(node) {
  const supported = Boolean(node.caps?.identify_supported);
  const active = (state.identifying[node.serial] || 0) > Date.now();
  const disabled = !supported || node.state === 'lost';
  const pattern = node.role === 'host'
    ? 'Blink the host MPU user LED'
    : `Blink ${node.slot} quick pulse${node.slot === 1 ? '' : 's'}, repeated twice`;
  const title = supported ? pattern : 'No writable MPU user LED on this board image';
  return `<button class="small identify${active ? ' identify-active' : ''}"
                  data-identify="${node.serial}" title="${title}"
                  ${disabled || active ? 'disabled' : ''}>${active ? 'Blinking…' : 'Identify'}</button>`;
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
    else if (d.identify) await identifyBoard(d.identify);
    else if (d.decommission) await decommission(d.decommission);
    else if (d.recommission) await api(`/api/nodes/${d.recommission}/recommission`, { method: 'POST' });
    else if (d.sudoSave) await sudoAction('save', d.sudoSave);
    else if (d.sudoForget) await sudoAction('forget', d.sudoForget);
    else if (d.sudoSet) await sudoAction('set', d.sudoSet);
    else if (d.unload || target.id === 'engine-stop') await api('/api/engine/stop', { method: 'POST' });
    else if (d.load) await loadModel(d.load);
    else if (target.id === 'rescan') await api('/api/nodes/rescan', { method: 'POST' });
    else if (target.id === 'provision-all') await api('/api/nodes/provision-all', { method: 'POST' });
  } catch (err) {
    alert(err.message);
  }
});

async function identifyBoard(serial) {
  const result = await api(`/api/nodes/${serial}/identify`, {
    method: 'POST',
    body: '{}',
  });
  const pulses = result.pulses || 1;
  const duration = serial === 'host' ? 3600 : 2 * (pulses * 440 + 650) + 500;
  state.identifying[serial] = Date.now() + duration;
  if (state.lastData) {
    renderNodes(state.lastData);
    renderSettings(state.lastData);
  }
  setTimeout(() => {
    delete state.identifying[serial];
    if (state.lastData) {
      renderNodes(state.lastData);
      renderSettings(state.lastData);
    }
  }, duration);
}

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
const BENCHMARK_PROMPTS = [
  { label: 'Speed: what is 12 + 30?', prompt: 'What is 12 + 30? Answer with just the number.' },
  { label: 'Speed: capital of France', prompt: 'Name the capital of France in one word.' },
  { label: 'Speed: prime check (7)', prompt: 'Is 7 a prime number? Answer yes or no.' },
  { label: 'Reasoning: train speed', prompt: 'If a train travels 60 miles in 45 minutes, what is its speed in mph? Answer with just the number.' },
  { label: 'Reasoning: sheep riddle', prompt: 'A farmer has 17 sheep. All but 9 are lost. How many are left? One number only.' },
  { label: 'Reasoning: 15% of 240', prompt: 'What is 15% of 240? Just the number.' },
  { label: 'Trap: two coins totaling 30 cents', prompt: 'I have two coins totaling 30 cents, and one is not a nickel. What are the two coins?' },
  { label: 'Trap: bat and ball', prompt: 'A bat and a ball cost $1.10 total. The bat costs $1 more than the ball. How much does the ball cost?' },
  { label: 'Trap: pound of feathers vs steel', prompt: 'Which is heavier: a pound of feathers or a pound of steel? One word.' },
  { label: 'Knowledge: Pride and Prejudice author', prompt: 'Who wrote "Pride and Prejudice"? Name only.' },
  { label: 'Knowledge: Berlin Wall year', prompt: 'What year did the Berlin Wall fall? Number only.' },
  { label: 'Knowledge: gold symbol', prompt: 'What is the chemical symbol for gold?' },
  { label: 'Instruction: first 4 primes', prompt: 'List the first 4 prime numbers, comma-separated.' },
  { label: 'Instruction: reverse "language"', prompt: 'Reverse the word "language".' },
  { label: 'Instruction: opposite of ephemeral', prompt: 'Give the opposite of "ephemeral" in one word.' },
  { label: 'Code: 3 // 2 in Python', prompt: 'In Python, what does 3 // 2 evaluate to? Number only.' },
  { label: 'Code: is "racecar" a palindrome?', prompt: 'Is the string "racecar" a palindrome? Yes or no.' },
  { label: 'Italian: translate "good morning"', prompt: 'Translate "good morning" to Italian. One phrase only.' },
  { label: 'Italian: translate "where is the train station?"', prompt: 'Translate "Where is the train station?" to Italian.' },
  { label: 'Italian: translate a sentence to English', prompt: 'Translate this Italian sentence to English: "Mi piacerebbe un caffè, per favore."' },
  { label: 'Italian: singular vs plural', prompt: 'What is the plural of the Italian word "amico"? One word.' },
];

$('benchmark-prompt').innerHTML = '<option value="">Choose a prompt to fill it in…</option>' +
  BENCHMARK_PROMPTS.map((p, i) => `<option value="${i}">${p.label}</option>`).join('');

$('benchmark-prompt').addEventListener('change', () => {
  const idx = $('benchmark-prompt').value;
  if (idx === '') return;
  $('prompt').value = BENCHMARK_PROMPTS[idx].prompt;
  $('prompt').focus();
  $('benchmark-prompt').value = '';
});

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

$('max-tokens').addEventListener('input', () => {
  $('max-tokens').dataset.userEdited = '1';
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
  const outboundHistory = [...state.history.slice(-8), { role: 'user', content: prompt }];
  const slowModel = (state.engine?.model_id || '').toLowerCase().includes('7b');
  const bubble = addMessage(
    'assistant pending',
    slowModel ? 'Waiting for first token… Mistral 7B can take around 30 seconds.' : 'Waiting for first token…'
  );
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
          ...outboundHistory,
        ],
        max_tokens: Number($('max-tokens').value),
        temperature: Number($('temperature').value),
        presence_penalty: 1.1,
        stream: true,
      }),
    });
    if (!response.ok) throw new Error(await responseError(response));

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
            bubble.className = 'msg assistant';
            bubble.textContent = answer;
            $('messages').scrollTop = $('messages').scrollHeight;
          }
        } catch { /* keepalive or partial frame */ }
      }
    }
    const elapsed = (performance.now() - started) / 1000;
    $('metrics').textContent =
      `${elapsed.toFixed(1)} s total · first token ${(firstToken / 1000 || 0).toFixed(1)} s`;
  } catch (err) {
    if (err.name !== 'AbortError') {
      bubble.className = 'msg error';
      bubble.textContent = err.message;
    }
  } finally {
    if (answer) {
      state.history.push({ role: 'user', content: prompt });
      state.history.push({ role: 'assistant', content: answer });
    }
    $('send').disabled = false;
    $('stop').hidden = true;
    state.controller = null;
    updateContextNote();
  }
});

async function responseError(response) {
  const text = await response.text();
  try {
    const body = JSON.parse(text);
    const message = body.error?.message || body.error || response.statusText;
    if (response.status === 429) {
      return `${message}. Wait for the current response to finish or press Stop.`;
    }
    return message;
  } catch {
    return text.slice(0, 300) || response.statusText;
  }
}

updateContextNote();
connect();
