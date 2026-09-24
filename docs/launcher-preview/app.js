'use strict';

// Independent prototype: no requests to ERP, review services, or launcher commands.
const icons = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  history: '<path d="M3 11a9 9 0 1 1 2 7M3 4v7h7"/><path d="M12 7v5l3 2"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-1 .5-1 1.2-1 2.2M12 17h.01"/>',
  spark: '<path d="m12 3 2.6 6.4L21 12l-6.4 2.6L12 21l-2.6-6.4L3 12l6.4-2.6L12 3ZM20 2v4M18 4h4"/>',
  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  sheet: '<path d="M14 3H5v18h14V8l-5-5Z"/><path d="M14 3v5h5M8 12h8M8 16h8M12 12v7"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.4"/><path d="m3 17 5-5 4 4 4-6 5 6"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
  edit: '<path d="m15 4 5 5M4 20l4-1 13-13a2 2 0 0 0-5-5L3 14l1 6Z"/>',
  save: '<path d="M4 3h13l4 4v14H3V3h1ZM7 3v6h9V3M7 21v-8h10v8"/>',
  upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 14v7h16v-7"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3"/>',
  route: '<circle cx="6" cy="5" r="2"/><circle cx="18" cy="19" r="2"/><path d="M8 5h7a4 4 0 0 1 0 8H9a3 3 0 0 0 0 6h7"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
  play: '<path d="m7 4 14 8-14 8V4Z"/>',
  arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
  up: '<path d="m6 14 6-6 6 6"/>',
  down: '<path d="m6 10 6 6 6-6"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  search: '<circle cx="10" cy="10" r="6.5"/><path d="m15 15 6 6"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  box: '<path d="m12 3 9 5v9l-9 5-9-5V8l9-5Zm0 10 9-5M3 8l9 5v9M7 6l10 5"/>',
  plus: '<path d="M12 4v16M4 12h16"/>',
  shirt: '<path d="m8 3-6 4 3 6 3-1v9h8v-9l3 1 3-6-6-4c0 4-8 4-8 0Z"/>',
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.box}</svg>`;
document.querySelectorAll('[data-icon]').forEach(el => { el.innerHTML = icon(el.dataset.icon); });
const $ = id => document.getElementById(id);
const platforms = [
  {id:'douyin', name:'抖音', mark:'<span>♪</span>'},
  {id:'taobao', name:'淘宝', mark:'淘'},
  {id:'tmall', name:'天猫', mark:'天'},
  {id:'pdd', name:'拼多多', mark:'拼'},
  {id:'wxsph', name:'微信小店', mark:'微'},
  {id:'xhs', name:'小红书', mark:'小红书'},
  {id:'youzan', name:'有赞', mark:'赞'},
  {id:'jd', name:'京东', mark:'JD'},
];
const platform = id => platforms.find(p => p.id === id);
const logo = p => `<span class="platform-logo ${p.id}" aria-hidden="true">${p.mark}</span>`;
const modes = [
  {id:'preview', name:'仅填写', description:'检查字段，不保存', icon:'edit', note:'填写与校验后结束，保留检查结果。'},
  {id:'save', name:'只保存', description:'保存资料，不提交店铺', icon:'save', note:'资料保存后结束，不提交到店铺。'},
  {id:'publish', name:'保存并铺货', description:'保存后提交指定店铺', icon:'upload', note:'正式流程会提交到已配置店铺；本页仅演示。'},
];
const products = [
  {id:'NGBL-2107', title:'复古做旧鼹鼠皮多口袋刺绣工装裤', category:'男装 / 休闲裤', image:'assets/product-2107.png'},
  {id:'DEMO-0002', title:'水洗棉质圆领短袖上衣', category:'示例商品 / 上衣', image:null},
  {id:'DEMO-0003', title:'轻薄连帽休闲夹克', category:'示例商品 / 外套', image:null},
];
const state = {selected:['douyin','jd','xhs'], product:products[0], mode:'save', learning:true, history:[], run:null};
let toastTimer;
const mode = () => modes.find(m => m.id === state.mode);
const productArt = product => product.image
  ? `<img src="${product.image}" alt="${product.title}">`
  : `<div class="demo-art">${icon('shirt')}</div>`;

function toast(message) {
  clearTimeout(toastTimer);
  $('toast').textContent = message;
  $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 2600);
}
function renderProduct() {
  $('product-image').innerHTML = productArt(state.product);
  $('product-code').textContent = state.product.id;
  $('product-title').textContent = state.product.title;
  $('product-category').textContent = state.product.category;
  $('plan-product').textContent = state.product.id;
}
function renderPlatformSelection() {
  document.querySelectorAll('[data-platform]').forEach(button => {
    button.setAttribute('aria-pressed', String(state.selected.includes(button.dataset.platform)));
  });
  $('selected-count').textContent = `已选 ${state.selected.length} 个`;
  renderPlan();
}
function renderPlan() {
  $('plan-count').textContent = `${state.selected.length} 个平台`;
  $('plan-list').innerHTML = state.selected.map((id,index) => {
    const p = platform(id);
    return `<li><span class="queue-number">${index+1}</span><span class="queue-logo">${logo(p)}</span><span class="queue-name">${p.name}</span><div class="reorder"><button data-move="${id}" data-direction="-1" aria-label="上移${p.name}" ${index === 0 ? 'disabled' : ''}>${icon('up')}</button><button data-move="${id}" data-direction="1" aria-label="下移${p.name}" ${index === state.selected.length-1 ? 'disabled' : ''}>${icon('down')}</button></div></li>`;
  }).join('');
  $('empty-plan').hidden = state.selected.length > 0;
  $('preview-run').disabled = state.selected.length === 0;
  $('plan-mode').textContent = mode().name;
  $('plan-learning').innerHTML = state.learning ? '<i></i>已开启' : '已关闭';
  $('mode-note').classList.toggle('publish', state.mode === 'publish');
  $('mode-note').innerHTML = icon(state.mode === 'publish' ? 'info' : 'shield') + `<p>${mode().note}</p>`;
}
function setMode(id) {
  state.mode = id;
  document.querySelectorAll('[data-mode]').forEach(button => {
    button.setAttribute('aria-checked', String(button.dataset.mode === id));
    button.tabIndex = button.dataset.mode === id ? 0 : -1;
  });
  renderPlan();
}
$('platform-grid').innerHTML = platforms.map(p => `<button class="platform-card" data-platform="${p.id}" aria-label="${p.name}" aria-pressed="false">${logo(p)}<span class="platform-name">${p.name}</span><span class="select-check">${icon('check')}</span></button>`).join('');
$('platform-grid').addEventListener('click', event => {
  const button = event.target.closest('[data-platform]');
  if (!button) return;
  const id = button.dataset.platform;
  state.selected = state.selected.includes(id) ? state.selected.filter(item => item !== id) : [...state.selected,id];
  renderPlatformSelection();
});
$('select-all').onclick = () => {
  state.selected = [...state.selected, ...platforms.map(p => p.id).filter(id => !state.selected.includes(id))];
  renderPlatformSelection();
};
$('clear-all').onclick = () => { state.selected = []; renderPlatformSelection(); };
$('plan-list').addEventListener('click', event => {
  const button = event.target.closest('[data-move]');
  if (!button || button.disabled) return;
  const index = state.selected.indexOf(button.dataset.move);
  const target = index + Number(button.dataset.direction);
  if (index < 0 || target < 0 || target >= state.selected.length) return;
  [state.selected[index],state.selected[target]] = [state.selected[target],state.selected[index]];
  renderPlan();
  const nextFocus = [...$('plan-list').querySelectorAll('[data-move]')].find(el => el.dataset.move === button.dataset.move && !el.disabled);
  nextFocus?.focus({preventScroll:true});
});
$('mode-grid').innerHTML = modes.map(m => `<button class="mode-card" role="radio" data-mode="${m.id}" aria-checked="false"><div class="mode-top">${icon(m.icon)}<span>${m.name}</span><span class="radio-mark"></span></div><p>${m.description}</p></button>`).join('');
$('mode-grid').addEventListener('click', event => {
  const button = event.target.closest('[data-mode]');
  if (button) setMode(button.dataset.mode);
});
$('mode-grid').addEventListener('keydown', event => {
  if (!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)) return;
  event.preventDefault();
  const index = modes.findIndex(m => m.id === state.mode);
  const direction = ['ArrowLeft','ArrowUp'].includes(event.key) ? -1 : 1;
  const next = modes[(index+direction+modes.length)%modes.length];
  setMode(next.id);
  document.querySelector(`[data-mode="${next.id}"]`).focus();
});
$('learning-toggle').onchange = event => { state.learning = event.target.checked; renderPlan(); };

function openDialog(id) { if (!$(id).open) $(id).showModal(); }
document.querySelectorAll('[data-close]').forEach(button => {
  button.onclick = () => $(button.dataset.close).close();
});
document.querySelectorAll('dialog').forEach(dialog => {
  dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom) return;
    dialog.id === 'run-dialog' ? closeRun() : dialog.close();
  });
});
$('nav-home').onclick = () => window.scrollTo({top:0,behavior:'smooth'});
document.querySelector('.brand').onclick = event => { event.preventDefault(); $('nav-home').click(); };
$('nav-help').onclick = () => openDialog('help-dialog');
$('more-button').onclick = () => openDialog('more-dialog');
function renderProductChoices() {
  const query = $('product-search').value.trim().toLowerCase();
  const filtered = products.filter(p => `${p.id} ${p.title} ${p.category}`.toLowerCase().includes(query));
  $('product-list').innerHTML = filtered.map(p => `<button class="product-choice ${state.product.id === p.id ? 'selected' : ''}" data-product="${p.id}"><span class="choice-image">${productArt(p)}</span><span class="choice-info"><strong>${p.title}</strong><p>${p.id} · ${p.category}</p></span>${state.product.id === p.id ? `<span class="choice-check">${icon('check')}</span>` : ''}</button>`).join('');
  $('product-empty').hidden = filtered.length > 0;
}
$('change-product').onclick = () => { $('product-search').value = ''; renderProductChoices(); openDialog('product-dialog'); $('product-search').focus(); };
$('product-search').oninput = renderProductChoices;
$('product-list').onclick = event => {
  const button = event.target.closest('[data-product]');
  if (!button) return;
  state.product = products.find(p => p.id === button.dataset.product);
  renderProduct();
  $('product-dialog').close();
};
function renderHistory() {
  $('history-count').textContent = String(state.history.length);
  if (!state.history.length) {
    $('history-list').innerHTML = '<div class="empty-message">还没有演示记录<br><br>选择平台后，点击“预览运行”试一试。</div>';
    return;
  }
  $('history-list').innerHTML = [...state.history].reverse().map(item => `<article class="history-item"><div><h3>${item.product.id}</h3><p>${item.ids.map(id => platform(id).name).join(' → ')}<br>${item.modeName} · ${item.time}</p></div><span>${item.finished ? '演示完成' : '演示已停止'}</span></article>`).join('');
}
$('nav-history').onclick = () => { renderHistory(); openDialog('history-dialog'); };
function renderRunStages() {
  const run = state.run;
  $('run-stages').innerHTML = run.ids.map((id,index) => {
    const done = index < run.step || run.phase === 'done';
    const current = !done && index === run.step && run.phase !== 'ready';
    const status = done ? `${icon('check')}已演示` : current ? (run.phase === 'paused' ? '已暂停' : '<span class="spinner"></span>演示中') : '等待演示';
    return `<div class="run-stage ${done ? 'done' : current ? 'running' : ''}">${logo(platform(id))}<span>${platform(id).name}</span><span class="run-stage-status">${status}</span></div>`;
  }).join('');
}
function recordRun(finished) {
  const run = state.run;
  if (run.recorded) return;
  run.recorded = true;
  state.history.push({product:run.product, ids:[...run.ids], modeName:run.modeName, finished, time:new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})});
  renderHistory();
}
function log(text) {
  $('run-log').textContent += `[模拟] ${text}\n`;
  $('run-log').scrollTop = $('run-log').scrollHeight;
}
function openRun() {
  if (!state.selected.length) return;
  if (state.run?.timer) clearTimeout(state.run.timer);
  state.run = {ids:[...state.selected], product:state.product, mode:state.mode, modeName:mode().name, learning:state.learning, step:-1, phase:'ready', timer:null, recorded:false};
  $('run-title').textContent = '确认这次运行';
  $('run-summary').innerHTML = `<span>商品</span><strong>${state.run.product.id}</strong><span>执行模式</span><strong>${state.run.modeName}</strong><span>人工审核</span><strong>${state.run.learning ? '开启' : '关闭'}</strong>`;
  $('run-confirm').innerHTML = `${icon('play')}开始演示`;
  $('run-confirm').disabled = false;
  $('run-secondary').textContent = '返回调整';
  $('run-progress').hidden = true;
  $('progress-fill').style.width = '0%';
  $('run-percent').textContent = '0%';
  $('log-details').hidden = true;
  $('log-details').open = false;
  $('run-log').textContent = '';
  renderRunStages();
  openDialog('run-dialog');
}
function advanceRun() {
  const run = state.run;
  if (!run || run.phase !== 'running') return;
  if (run.step >= 0) log(`${platform(run.ids[run.step]).name}：${run.modeName}流程演示完成`);
  run.step += 1;
  const percent = Math.round(run.step / run.ids.length * 100);
  $('progress-fill').style.width = `${percent}%`;
  $('run-percent').textContent = `${percent}%`;
  if (run.step === run.ids.length) {
    run.phase = 'done';
    $('run-title').textContent = '演示已完成';
    $('run-progress-label').textContent = `${run.ids.length} 个平台已完成演示`;
    $('run-secondary').textContent = '关闭';
    $('run-confirm').textContent = '再演示一次';
    $('run-confirm').disabled = false;
    log('本次模拟结束，未操作真实商品或店铺。');
    recordRun(true);
  } else {
    $('run-progress-label').textContent = `正在演示 ${platform(run.ids[run.step]).name}`;
    log(`${platform(run.ids[run.step]).name}：准备商品资料与属性填写`);
    run.timer = setTimeout(advanceRun, 1100);
  }
  renderRunStages();
}
function startRun() {
  const run = state.run;
  $('run-progress').hidden = false;
  $('log-details').hidden = false;
  $('run-confirm').textContent = '演示中…';
  $('run-confirm').disabled = true;
  $('run-secondary').textContent = '暂停演示';
  $('run-title').textContent = '体验一次完整运行';
  const resuming = run.phase === 'paused';
  run.phase = 'running';
  if (resuming) {
    log('继续演示');
    $('run-progress-label').textContent = `正在演示 ${platform(run.ids[run.step]).name}`;
    run.timer = setTimeout(advanceRun, 1100);
    renderRunStages();
  } else {
    log(`开始演示：${run.product.id}，共 ${run.ids.length} 个平台`);
    advanceRun();
  }
}
function closeRun() {
  const run = state.run;
  if (run) {
    clearTimeout(run.timer);
    if (run.phase === 'running' || run.phase === 'paused') {
      run.phase = 'stopped';
      recordRun(false);
      toast('演示已停止');
    }
  }
  $('run-dialog').close();
}
$('preview-run').onclick = openRun;
$('close-run').onclick = closeRun;
$('run-dialog').addEventListener('cancel', event => { event.preventDefault(); closeRun(); });
$('run-confirm').onclick = () => state.run.phase === 'done' ? openRun() : startRun();
$('run-secondary').onclick = () => {
  const run = state.run;
  if (run.phase === 'running') {
    clearTimeout(run.timer);
    run.phase = 'paused';
    $('run-progress-label').textContent = '演示已暂停';
    $('run-confirm').textContent = '继续演示';
    $('run-confirm').disabled = false;
    $('run-secondary').textContent = '结束演示';
    log('演示暂停');
    renderRunStages();
  } else closeRun();
};
renderProduct();
renderPlatformSelection();
setMode('save');
