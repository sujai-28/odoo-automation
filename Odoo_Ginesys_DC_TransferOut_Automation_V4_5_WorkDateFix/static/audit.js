const audit$ = (selector) => document.querySelector(selector);
const auditEsc = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));

function auditDate(value) {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

async function loadAudit() {
  const response = await fetch('/api/audit-events?limit=500');
  const data = await response.json();
  if (!response.ok || !data.success) throw new Error(data.error || 'Could not load audit events.');
  audit$('#audit-body').innerHTML = data.events.map((event) => `
    <tr>
      <td>${auditEsc(auditDate(event.occurred_at))}</td>
      <td>${auditEsc(event.ginesys_username || '-')}</td>
      <td>${auditEsc(event.display_name || '-')}</td>
      <td>${auditEsc(event.action)}</td>
      <td><span class="status-pill ${['SUCCESS'].includes(event.status) ? 'pass' : 'fail'}">${auditEsc(event.status)}</span></td>
      <td>${auditEsc(event.details || '-')}</td>
      <td>${auditEsc(event.ip_address || '-')}</td>
    </tr>
  `).join('') || '<tr><td colspan="7">No activity recorded yet.</td></tr>';
}

async function loadHeaderConnection() {
  const response = await fetch('/api/config-status');
  const data = await response.json();
  if (!response.ok || !data.success) throw new Error(data.error || 'Connection status unavailable.');
  audit$('#user-auth-time').textContent = `Last authenticated: ${auditDate(data.lastAuthenticatedAt)}`;
}

async function testConnection() {
  const button = audit$('#test-connection-btn');
  button.disabled = true;
  try {
    const response = await fetch('/api/check-session');
    const data = await response.json();
    if (!response.ok || !data.success) throw new Error(data.error || 'Connection failed.');
    await loadHeaderConnection();
    await loadAudit();
  } catch (error) {
    audit$('#user-auth-time').textContent = 'Connection failed • click name to retry';
  } finally {
    button.disabled = false;
  }
}

audit$('#logout-btn')?.addEventListener('click', async () => {
  await fetch('/api/logout', {method: 'POST'});
  window.location.href = '/login';
});
audit$('#test-connection-btn')?.addEventListener('click', testConnection);
audit$('#refresh-audit-btn')?.addEventListener('click', () => loadAudit().catch((error) => alert(error.message)));

Promise.all([loadAudit(), loadHeaderConnection()]).catch((error) => alert(error.message));
