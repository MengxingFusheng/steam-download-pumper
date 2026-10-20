(() => {
  'use strict';

  const dialog = document.createElement('dialog');
  dialog.id = 'renameDialog';
  dialog.className = 'rename-dialog';
  dialog.setAttribute('aria-labelledby', 'renameTitle');
  dialog.setAttribute('aria-describedby', 'renameDescription');
  dialog.innerHTML = `<form class="rename-form" novalidate>
    <p class="eyebrow">NODE SETTINGS</p><h2 id="renameTitle">编辑节点名称</h2>
    <p id="renameDescription" class="muted rename-copy">名称用于识别和搜索节点。</p>
    <label for="renameInput">节点名称</label>
    <input id="renameInput" name="name" type="text" required autocomplete="off" autofocus aria-describedby="renameCount renameError">
    <p id="renameCount" class="muted rename-count"></p>
    <div id="renameError" class="notice error" role="alert" hidden></div>
    <div class="actions"><button type="button" class="secondary" id="renameCancel">取消</button><button type="submit" id="renameSave">保存名称</button></div>
  </form>`;
  document.body.append(dialog);

  const form = dialog.querySelector('form');
  const input = dialog.querySelector('#renameInput');
  const counter = dialog.querySelector('#renameCount');
  const error = dialog.querySelector('#renameError');
  const cancel = dialog.querySelector('#renameCancel');
  const save = dialog.querySelector('#renameSave');
  let nodeId = null;
  let loading = false;
  let returnFocus = null;

  function count() {
    counter.textContent = `${Array.from(input.value.trim()).length} / 100 个字符`;
  }

  function showError(message) {
    error.textContent = message;
    error.hidden = false;
    input.setAttribute('aria-invalid', 'true');
  }

  function busy(value) {
    loading = value;
    input.disabled = value;
    cancel.disabled = value;
    save.disabled = value;
    save.textContent = value ? '正在保存…' : '保存名称';
    form.setAttribute('aria-busy', String(value));
  }

  input.addEventListener('input', () => {
    error.hidden = true;
    input.removeAttribute('aria-invalid');
    count();
  });
  cancel.addEventListener('click', () => dialog.close());
  dialog.addEventListener('cancel', event => {
    if (loading) event.preventDefault();
  });
  dialog.addEventListener('close', () => {
    if (returnFocus?.isConnected) returnFocus.focus();
  });

  window.addEventListener('fleet-rename-node', event => {
    if (loading || typeof event.detail?.id !== 'string') return;
    nodeId = event.detail.id;
    returnFocus = document.activeElement;
    input.value = typeof event.detail.name === 'string' ? event.detail.name : '';
    error.hidden = true;
    input.removeAttribute('aria-invalid');
    count();
    if (!dialog.open) dialog.showModal();
    input.focus();
    input.select();
  });

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (loading || !nodeId) return;
    const name = input.value.trim();
    if (!name || Array.from(name).length > 100 || /[\u0000-\u001f\u007f]/u.test(name)) {
      showError(!name ? '请输入节点名称。' : '名称最多 100 个字符，且不能包含控制字符。');
      input.focus();
      return;
    }
    busy(true);
    try {
      const response = await fetch(`/api/v1/nodes/${encodeURIComponent(nodeId)}`, {
        method: 'PATCH',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'FleetDashboard' },
        body: JSON.stringify({ name }),
        signal: AbortSignal.timeout(15000),
      });
      if (!response.ok) {
        const messages = {
          401: '登录已失效，请刷新并重新登录。',
          403: '无权修改节点名称，请刷新后重试。',
          404: '未找到该节点，请刷新节点列表。',
          422: '名称格式不正确，请使用 1 至 100 个字符。',
        };
        throw new Error(messages[response.status] || '保存未完成，请刷新后确认节点名称。');
      }
      const node = await response.json();
      busy(false);
      dialog.close();
      window.dispatchEvent(new CustomEvent('fleet-node-renamed', { detail: node }));
    } catch (failure) {
      showError(failure.name === 'TimeoutError' || failure.name === 'TypeError'
        ? '连接中断，请刷新后确认节点名称。'
        : failure.message);
    } finally {
      busy(false);
      if (dialog.open) input.focus();
    }
  });
})();
