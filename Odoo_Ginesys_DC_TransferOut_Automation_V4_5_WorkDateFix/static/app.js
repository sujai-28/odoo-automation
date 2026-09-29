const $ = (s) => document.querySelector(s);
let currentJobId = null;
let tokenConfigured = false;
let postingLocked = false;

const form = $('#upload-form');
const filesInput = $('#files');
const fileList = $('#file-list');
const validationCard = $('#validation-card');
const postingCard = $('#posting-card');
const validateBtn = $('#validate-btn');
const postBtn = $('#post-btn');
const retryBtn = $('#retry-btn');
const confirmPost = $('#confirm-post');

function esc(v) { return String(v ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
function fmtQty(v) { return Number(v || 0).toLocaleString(undefined, {maximumFractionDigits: 2}); }
function metric(label, value, tone='') { return `<div class="metric ${tone}"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`; }
function hasCompletedGinesysCheck(data) {
  const validStatuses = new Set(['TRANSFER_EXISTS', 'DC_EXISTS', 'NOT_FOUND']);
  return data?.ginesysCheckCompleted === true && data?.ginesysCheckVersion === 1 &&
    Array.isArray(data.preview) && data.preview.every(row => validStatuses.has(row.ginesysStatus));
}
function frontendErrorMessage(error) {
  const message = String(error || '').trim();
  if (!message) return '';
  const lower = message.toLowerCase();
  if (lower.includes('duplicate value of document no') || lower.includes('document number already')) {
    return 'Document number already exists.';
  }
  if (lower.includes('document numbering scheme') && lower.includes('inappropriate')) {
    return 'Document numbering scheme is inappropriate.';
  }
  if (lower.includes('gst state metadata is missing')) {
    return 'GST state metadata is missing.';
  }
  if (message.length > 140 || lower.includes('/webapi/api/')) {
    return 'Posting failed. Download result file for details.';
  }
  return message;
}

async function logout() {
  await fetch('/api/logout', {method:'POST'});
  window.location.href = '/login';
}

$('#logout-btn')?.addEventListener('click', logout);

async function loadConfig() {
  const authTime = $('#user-auth-time');
  try {
    const r = await fetch('/api/config-status');
    const d = await r.json();
    if (!d.success) throw new Error(d.error);
    tokenConfigured = d.tokenConfigured;
    authTime.textContent = `Last authenticated: ${d.lastAuthenticatedAt ? new Date(d.lastAuthenticatedAt).toLocaleString() : 'never'}`;
  } catch (e) {
    authTime.textContent = `Connection unavailable • click name to retry`;
  }
}

$('#test-connection-btn')?.addEventListener('click', async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const response = await fetch('/api/check-session');
    const data = await response.json();
    if (!response.ok || !data.success) throw new Error(data.error || (data.errors || []).join('; ') || 'Connection failed.');
    alert(data.message);
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    await loadConfig();
  }
});

filesInput.addEventListener('change', () => { fileList.textContent = [...filesInput.files].map(f => f.name).join(' | '); });
confirmPost.addEventListener('change', () => { postBtn.disabled = postingLocked || !confirmPost.checked; });
const zone = $('#file-zone');
['dragenter','dragover'].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add('over'); }));
['dragleave','drop'].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.remove('over'); }));
zone.addEventListener('drop', e => {
  const dt = new DataTransfer();
  [...e.dataTransfer.files].forEach(f => dt.items.add(f));
  filesInput.files = dt.files;
  fileList.textContent = [...filesInput.files].map(f => f.name).join(' | ');
});

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  if (!filesInput.files.length) return;
  validateBtn.disabled = true;
  validateBtn.querySelector('span').textContent = 'Validating...';
  validationCard.hidden = true;
  postingCard.hidden = true;
  currentJobId = null;
  postingLocked = false;
  confirmPost.checked = false;
  postBtn.disabled = true;
  retryBtn.hidden = true;
  retryBtn.disabled = true;
  try {
    const data = new FormData(form);
    const r = await fetch('/api/validate', {method:'POST', body:data});
    const d = await r.json();
    if (!r.ok || !d.success) throw new Error(d.error || 'Validation failed.');
    if (!hasCompletedGinesysCheck(d)) {
      throw new Error('The updated Ginesys duplicate check is not active. Restart the portal, refresh this page, and validate the files again. Posting remains blocked.');
    }
    currentJobId = d.jobId;
    renderValidation(d);
  } catch (err) { alert(err.message); }
  finally {
    validateBtn.disabled = false;
    validateBtn.querySelector('span').textContent = 'Validate & Build Posting Preview';
  }
});

function renderValidation(d) {
  validationCard.hidden = false;
  $('#metrics').innerHTML = [
    metric('EBO Stores', d.summary.stores ?? 0),
    metric('Odoo Documents', d.summary.documents),
    metric('Already Created', d.summary.alreadyCreated, d.summary.alreadyCreated ? 'good' : ''),
    metric('Total Qty', fmtQty(d.summary.totalQty)),
    metric('Valid Source Lines', d.summary.validLines),
    metric('Combo Lines', d.summary.comboLines)
  ].join('');
  $('#preview-body').innerHTML = d.preview.map(x => { const exists = x.ginesysStatus === 'TRANSFER_EXISTS'; const dcOnly = x.ginesysStatus === 'DC_EXISTS'; const missing = x.ginesysStatus === 'NOT_FOUND'; const label = exists ? 'Already Created' : (dcOnly ? 'Transfer Pending' : (missing ? 'New Document' : 'Check Unavailable')); return `<tr><td>${esc(x.reference)}</td><td>${esc(x.date)}</td><td>${esc(x.store)}</td><td>${esc(x.siteCode)}</td><td><span class="status-pill ${exists ? 'pass' : (dcOnly ? 'pending' : (missing ? 'clear' : 'fail'))}">${esc(label)}</span></td><td>${esc(x.dcNumber || '-')}</td><td>${esc(x.transferNumber || '-')}</td><td>${esc(x.state || '-')}</td><td>${esc(x.gstAppl)}</td><td>${x.gstinConfigured ? 'Configured' : 'Missing'}</td><td>${esc(x.docCode)}</td><td>${esc(x.lines)}</td><td>${esc(fmtQty(x.qty))}</td></tr>`; }).join('');
  const checkText = `${d.summary.alreadyCreated} already created, ${d.summary.dcOnly} awaiting Transfer Out, ${d.summary.notFound} new document(s).`;
  $('#blocking-area').innerHTML = `<div class="callout good-callout"><strong>Ginesys duplicate check completed.</strong><span>${esc(checkText)} ${esc(d.rateHandling)}</span></div>`;
  $('#warning-area').innerHTML = d.warnings.length ? `<details><summary>${d.warnings.length} configuration/data warning(s)</summary><ul>${d.warnings.map(w => `<li>${esc(w)}</li>`).join('')}</ul></details>` : '';
  $('#validation-message').textContent = d.ready ? 'Validation passed.' : 'Validation completed with blocking items.';
  const live = $('#live-panel');
  live.hidden = !d.ready || d.summary.postingNeeded === 0;
  if (d.ready && d.summary.postingNeeded > 0) {
    postBtn.disabled = postingLocked || !confirmPost.checked;
    if (!tokenConfigured) {
      live.innerHTML = `<div class="callout warn-callout"><strong>Validation passed, but your Ginesys credentials are not configured.</strong><span>Ask an administrator to add your Ginesys username and password, then use Test Ginesys Connection.</span></div>`;
    }
  }
  validationCard.scrollIntoView({behavior:'smooth', block:'start'});
}

async function postCurrent() {
  if (!currentJobId) return;
  postBtn.disabled = true;
  retryBtn.disabled = true;
  postBtn.querySelector('span').textContent = 'POSTING...';
  try {
    const r = await fetch(`/api/post/${currentJobId}`, {method:'POST'});
    const d = await r.json();
    if (!r.ok || !d.success) throw new Error(d.error || 'Posting failed.');
    renderPosting(d);
  } catch (e) { alert(e.message); }
  finally {
    postBtn.disabled = !postingCard.hidden || postingLocked || !confirmPost.checked;
    retryBtn.disabled = postingLocked || retryBtn.hidden;
    postBtn.querySelector('span').textContent = 'POST TO GINESYS';
  }
}

postBtn.addEventListener('click', postCurrent);
retryBtn.addEventListener('click', postCurrent);

function renderPosting(d) {
  postingCard.hidden = false;
  $('#result-download').href = d.downloadUrl;
  $('#post-message').textContent = `${d.summary.success} successful | ${d.summary.failed} failed`;
  $('#post-metrics').innerHTML = [metric('Documents', d.summary.total), metric('Successful', d.summary.success, 'good'), metric('Failed', d.summary.failed, d.summary.failed ? 'bad' : '')].join('');
  $('#result-body').innerHTML = d.results.map(r => { const failed = !['SUCCESS','ALREADY_POSTED'].includes(String(r.status)); return `<tr><td>${esc(r.reference)}</td><td>${esc(r.store_name)}</td><td>${esc(r.dc_number || r.dc_code || '-')}</td><td>${esc(r.dc_created_by_ginesys_username || '-')}</td><td>${esc(r.transfer_number || r.transfer_code || '-')}</td><td>${esc(r.transfer_created_by_ginesys_username || '-')}</td><td><span class="status-pill ${failed ? 'fail' : 'pass'}">${esc(r.status)}</span></td><td class="error-cell">${esc(frontendErrorMessage(r.error))}</td></tr>`; }).join('');
  postingLocked = d.summary.failed === 0;
  postBtn.disabled = true;
  retryBtn.hidden = false;
  retryBtn.disabled = d.summary.failed === 0;
  postingCard.scrollIntoView({behavior:'smooth', block:'start'});
}

loadConfig();
