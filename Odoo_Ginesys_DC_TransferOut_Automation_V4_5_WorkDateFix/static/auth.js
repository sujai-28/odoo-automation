const auth$ = (s) => document.querySelector(s);
let adminUsersById = new Map();

function showError(message) {
  const box = auth$('#form-error');
  if (!box) {
    alert(message);
    return;
  }
  box.hidden = false;
  box.textContent = message;
}

function formPayload(form) {
  return Object.fromEntries(new FormData(form).entries());
}

async function postJson(url, payload = {}) {
  const response = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.success === false) {
    const error = new Error(data.error || 'Request failed.');
    Object.assign(error, data);
    throw error;
  }
  return data;
}

async function authenticateWithGinesys(url, payload) {
  try {
    return await postJson(url, payload);
  } catch (error) {
    if (!error.requiresSessionLogout) throw error;
    const approved = window.confirm(`${error.message}\n\nDo you want to log out the existing Ginesys Web session and sign in here?`);
    if (!approved) throw new Error('Login cancelled. Your existing Ginesys Web session was left active.');
    return postJson(url, {...payload, logout_existing_session: true});
  }
}

async function logout() {
  await postJson('/api/logout');
  window.location.href = '/login';
}

auth$('#logout-btn')?.addEventListener('click', logout);

auth$('#login-form')?.addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    await authenticateWithGinesys('/api/login', formPayload(event.currentTarget));
    window.location.href = '/';
  } catch (error) {
    showError(error.message);
  }
});

auth$('#setup-form')?.addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    await authenticateWithGinesys('/api/setup', formPayload(event.currentTarget));
    window.location.href = '/';
  } catch (error) {
    showError(error.message);
  }
});

async function loadUsers() {
  const body = auth$('#users-body');
  if (!body) return;
  const response = await fetch('/api/admin/users');
  const data = await response.json();
  if (!response.ok || !data.success) throw new Error(data.error || 'Could not load users.');
  adminUsersById = new Map(data.users.map((user) => [String(user.id), user]));
  body.innerHTML = data.users.map((user) => `
    <tr>
      <td>${escapeHtml(user.ginesys_username || '-')}</td>
      <td>${escapeHtml(user.display_name)}</td>
      <td><span class="status-pill ${user.ginesys_connection_status === 'connected' ? 'pass' : 'fail'}">${escapeHtml((user.ginesys_connection_status || 'not configured').replaceAll('_', ' '))}</span></td>
      <td>${escapeHtml(formatDate(user.ginesys_last_authenticated_at))}</td>
      <td>${escapeHtml(user.role)}</td>
      <td><span class="status-pill ${user.active ? 'pass' : 'fail'}">${user.active ? 'Active' : 'Inactive'}</span></td>
      <td class="action-cell">
        <button class="row-action" data-edit-user-id="${user.id}" type="button">Edit</button>
        <button class="row-action" data-user-id="${user.id}" data-active="${user.active ? '0' : '1'}" type="button">${user.active ? 'Deactivate' : 'Activate'}</button>
      </td>
    </tr>
  `).join('');
}

function formatDate(value) {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
}

auth$('#user-form')?.addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const payload = formPayload(event.currentTarget);
    const editingId = payload.id;
    if (editingId) {
      await postJson(`/api/admin/users/${editingId}`, payload);
      resetUserForm();
    } else {
      await postJson('/api/admin/users', payload);
      event.currentTarget.reset();
    }
    const box = auth$('#form-error');
    if (box) box.hidden = true;
    await loadUsers();
  } catch (error) {
    showError(error.message);
  }
});

auth$('#users-body')?.addEventListener('click', async (event) => {
  const editButton = event.target.closest('button[data-edit-user-id]');
  if (editButton) {
    const user = adminUsersById.get(String(editButton.dataset.editUserId));
    if (!user) {
      showError('Could not find this user in the current admin list. Refresh the page and try again.');
      return;
    }
    const form = auth$('#user-form');
    form.elements['id'].value = user.id;
    form.elements['display_name'].value = user.display_name;
    form.elements['ginesys_username'].value = user.ginesys_username || '';
    form.elements['role'].value = user.role;
    auth$('#save-user-btn span').textContent = 'Update User';
    auth$('#save-user-btn b').textContent = 'ok';
    auth$('#cancel-edit-btn').hidden = false;
    form.scrollIntoView({behavior: 'smooth', block: 'start'});
    return;
  }
  const button = event.target.closest('button[data-user-id]');
  if (!button) return;
  button.disabled = true;
  try {
    await postJson(`/api/admin/users/${button.dataset.userId}/active`, {active: button.dataset.active === '1'});
    await loadUsers();
  } catch (error) {
    showError(error.message);
    button.disabled = false;
  }
});

function resetUserForm() {
  const form = auth$('#user-form');
  if (!form) return;
  form.reset();
  form.elements['id'].value = '';
  auth$('#save-user-btn span').textContent = 'Create User';
  auth$('#save-user-btn b').textContent = '+';
  auth$('#cancel-edit-btn').hidden = true;
}

auth$('#cancel-edit-btn')?.addEventListener('click', resetUserForm);

async function loadHeaderConnection() {
  if (!auth$('#user-auth-time')) return;
  const response = await fetch('/api/config-status');
  const data = await response.json();
  if (!response.ok || !data.success) throw new Error(data.error || 'Connection status unavailable.');
  auth$('#user-auth-time').textContent = `Last authenticated: ${formatDate(data.lastAuthenticatedAt)}`;
}

auth$('#test-connection-btn')?.addEventListener('click', async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const response = await fetch('/api/check-session');
    const data = await response.json();
    if (!response.ok || !data.success) throw new Error(data.error || 'Connection failed.');
    await loadHeaderConnection();
    await loadUsers();
  } catch (error) {
    auth$('#user-auth-time').textContent = `Connection failed • click name to retry`;
  } finally {
    button.disabled = false;
  }
});

loadUsers().catch((error) => showError(error.message));
loadHeaderConnection().catch((error) => {
  if (auth$('#user-auth-time')) auth$('#user-auth-time').textContent = 'Connection unavailable • click name to retry';
});
