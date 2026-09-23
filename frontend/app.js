(function () {
  const $ = (sel) => document.querySelector(sel);

  // crypto.randomUUID() only exists in a "secure context" (localhost/127.0.0.1
  // over HTTP, or any HTTPS origin) — it's undefined if this page is loaded
  // from e.g. http://0.0.0.0:8000 (a bind address, not a real host) or an
  // older browser. Fall back to a plain Math.random UUID so the app still
  // works regardless of hostname; these ids are just client-side correlation
  // ids, not security tokens, so this fallback is perfectly fine.
  function newId() {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID();
    }
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0;
      const v = c === 'x' ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  const state = {
    conversationId: null,
    busy: false,
    // Usage-history dropdown for the current pending free-text field
    // (job/warehouse/material/wbs — see app/tools/pr_intake_tool.py:
    // HISTORY_FIELDS). null = the current question doesn't have one at all;
    // [] = it does, just nothing recorded yet; [...] = pick from these.
    fieldHistory: null,
  };

  const messagesEl = $('#messages');
  const traceLogEl = $('#trace-log');
  const traceStatusEl = $('#trace-status');
  const form = $('#chat-form');
  const input = $('#chat-input');
  const sendBtn = $('#send-btn');
  const newConvBtn = $('#new-conv-btn');
  const llmBadge = $('#llm-badge');
  const convBadge = $('#conv-badge');
  const historyDropdown = $('#history-dropdown');

  function shortId(id) {
    return id ? id.slice(0, 8) : '—';
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function scrollToBottom(container) {
    container.scrollTop = container.scrollHeight;
  }

  // ---------------- chat rendering ----------------

  function appendUserMessage(text) {
    messagesEl.appendChild(el('div', 'msg msg-user', text));
    scrollToBottom(messagesEl);
  }

  function appendSystemMessage(text) {
    messagesEl.appendChild(el('div', 'msg msg-system', text));
    scrollToBottom(messagesEl);
  }

  let thinkingEl = null;
  function showThinking() {
    thinkingEl = el('div', 'thinking', 'thinking…');
    messagesEl.appendChild(thinkingEl);
    scrollToBottom(messagesEl);
  }
  function hideThinking() {
    if (thinkingEl) {
      thinkingEl.remove();
      thinkingEl = null;
    }
  }

  function buildTicketPrompt(messageId, offerText, onCancel) {
    const box = el('div', 'ticket-prompt');
    box.appendChild(el('div', null, offerText || 'Want me to raise a support ticket?'));
    const textarea = document.createElement('textarea');
    textarea.placeholder = 'Optional: add detail about what was wrong…';
    box.appendChild(textarea);

    const actions = el('div', 'ticket-prompt-actions');
    const raiseBtn = el('button', 'raise-btn', 'Raise support ticket');
    const cancelBtn = el('button', 'cancel-btn', 'Cancel');
    actions.appendChild(raiseBtn);
    actions.appendChild(cancelBtn);
    box.appendChild(actions);

    raiseBtn.addEventListener('click', async () => {
      raiseBtn.disabled = true;
      cancelBtn.disabled = true;
      raiseBtn.textContent = 'Raising ticket…';
      await raiseTicket(messageId, textarea.value.trim());
      box.remove();
    });

    cancelBtn.addEventListener('click', () => {
      box.remove();
      if (onCancel) onCancel();
    });

    return box;
  }

  // ---------------- field-history dropdown (job/warehouse/material/wbs) ----------------
  // Shows on focusing the composer input, sourced from ChatResponse.history
  // (app/store.py:get_field_history) for whichever field is currently
  // pending — not a fixed menu like appendOptionMenu, since these fields
  // have many possible values, just some worth surfacing as shortcuts.

  function renderHistoryDropdown() {
    historyDropdown.innerHTML = '';
    if (!state.fieldHistory.length) {
      historyDropdown.appendChild(el('div', 'history-empty', 'No recent values yet'));
      return;
    }
    state.fieldHistory.forEach((item) => {
      const btn = el('button', 'history-item', item.label);
      btn.type = 'button';
      // mousedown (not click) fires before the input's blur — handling
      // selection here, with preventDefault to keep focus, means the
      // dropdown doesn't get hidden by blur before the selection registers.
      btn.addEventListener('mousedown', (e) => {
        e.preventDefault();
        hideHistoryDropdown();
        sendMessage(item.value);
      });
      historyDropdown.appendChild(btn);
    });
  }

  function showHistoryDropdown() {
    if (state.fieldHistory === null) return;
    renderHistoryDropdown();
    historyDropdown.hidden = false;
  }

  function hideHistoryDropdown() {
    historyDropdown.hidden = true;
  }

  function appendOptionMenu(wrap, options) {
    // Fixed-choice PR-intake questions (app/tools/pr_intake_tool.py:options_for())
    // come with a `value` per button — clicking one sends it back exactly
    // like the customer had typed it, through the normal sendMessage() path,
    // so it's guaranteed to resolve. Typing is still available as a fallback.
    const menu = el('div', 'option-menu');
    const buttons = [];
    options.forEach((opt) => {
      const btn = el('button', 'option-btn', opt.label);
      btn.addEventListener('click', () => {
        if (state.busy) return;
        buttons.forEach((b) => (b.disabled = true));
        btn.classList.add('chosen');
        sendMessage(opt.value);
      });
      buttons.push(btn);
      menu.appendChild(btn);
    });
    wrap.appendChild(menu);
  }

  function appendPrDownloads(wrap, prId) {
    // Plain <a> links to app/main.py's GET /purchase-requisitions/{id}/pdf
    // and /payload — this is a real server, not a sandboxed preview, so a
    // normal browser download/new-tab-view works with no extra plumbing.
    const menu = el('div', 'option-menu');

    const previewLink = el('a', 'option-btn preview-btn', 'Preview');
    previewLink.href = `/purchase-requisitions/${prId}/pdf?inline=1`;
    previewLink.target = '_blank';
    previewLink.rel = 'noopener';

    const pdfLink = el('a', 'option-btn download-btn', '⬇ Download PDF');
    pdfLink.href = `/purchase-requisitions/${prId}/pdf`;
    pdfLink.setAttribute('download', '');

    const jsonLink = el('a', 'option-btn download-btn', '⬇ Download payload (JSON)');
    jsonLink.href = `/purchase-requisitions/${prId}/payload`;
    jsonLink.setAttribute('download', '');

    menu.appendChild(previewLink);
    menu.appendChild(pdfLink);
    menu.appendChild(jsonLink);
    wrap.appendChild(menu);
  }

  function appendAssistantMessage(data) {
    // ?? (not ||) so an empty-but-applicable history list ([]) isn't
    // collapsed into "not applicable" (null) — both are falsy under ||.
    state.fieldHistory = data.history ?? null;
    hideHistoryDropdown();

    const wrap = el('div', 'msg msg-assistant');
    wrap.appendChild(el('div', null, data.answer));
    wrap.appendChild(el('div', 'msg-meta', `${data.agent_used} · intent: ${data.intent}`));

    if (data.options && data.options.length) {
      appendOptionMenu(wrap, data.options);
    }

    if (data.pr_number && data.pr_id) {
      appendPrDownloads(wrap, data.pr_id);
    }

    const actions = el('div', 'msg-actions');
    const upBtn = el('button', 'icon-btn', '👍');
    const downBtn = el('button', 'icon-btn', '👎');
    actions.appendChild(upBtn);
    actions.appendChild(downBtn);
    wrap.appendChild(actions);

    let ticketPromptShown = false;

    async function handleFeedback(helpful, clickedBtn, otherBtn) {
      clickedBtn.disabled = true;
      otherBtn.disabled = true;
      clickedBtn.classList.add(helpful ? 'active-up' : 'active-down');
      try {
        const res = await fetch('/feedback', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            conversation_id: state.conversationId,
            message_id: data.message_id,
            helpful,
          }),
        });
        const fb = await res.json();
        if (!helpful && fb.offer_ticket && !ticketPromptShown) {
          ticketPromptShown = true;
          const box = buildTicketPrompt(data.message_id, fb.message, () => {
            // Cancelled — maybe thumbs-down was a mis-click. Let them re-vote.
            ticketPromptShown = false;
            clickedBtn.disabled = false;
            otherBtn.disabled = false;
            clickedBtn.classList.remove('active-up', 'active-down');
          });
          wrap.appendChild(box);
          scrollToBottom(messagesEl);
        }
      } catch (err) {
        appendSystemMessage('Feedback request failed: ' + err.message);
      }
    }

    upBtn.addEventListener('click', () => handleFeedback(true, upBtn, downBtn));
    downBtn.addEventListener('click', () => handleFeedback(false, downBtn, upBtn));

    messagesEl.appendChild(wrap);
    scrollToBottom(messagesEl);
  }

  // ---------------- trace rendering ----------------

  function traceDivider(label) {
    traceLogEl.appendChild(el('div', 'trace-divider', label));
    scrollToBottom(traceLogEl);
  }

  function agentClass(ev) {
    // trace rows have no explicit "status" column — an error step is
    // distinguished by carrying an "error" key in its payload (see
    // app/tracing.py's except-branch payload).
    if (ev.payload && ev.payload.error) return 'status-error';
    const agent = (ev.agent || 'tool').toLowerCase().replace(/\s+/g, '');
    return 'agent-' + agent;
  }

  function formatPayload(payload) {
    if (!payload || Object.keys(payload).length === 0) return null;
    return Object.entries(payload)
      .map(([k, v]) => `${k}: ${typeof v === 'object' && v !== null ? JSON.stringify(v) : v}`)
      .join('  ·  ');
  }

  function appendTraceLine(ev) {
    const line = el('div', 'trace-line ' + agentClass(ev));
    const label = ev.agent || 'tool';
    const head = el('div', 't-head');
    head.innerHTML =
      `<span class="t-step">#${ev.step}</span> <strong>${label}</strong> — ${ev.action}` +
      (ev.duration_ms != null ? `<span class="t-duration">${ev.duration_ms}ms</span>` : '');
    line.appendChild(head);

    const payloadText = formatPayload(ev.payload);
    if (payloadText) line.appendChild(el('div', 't-payload', payloadText));

    traceLogEl.appendChild(line);
    scrollToBottom(traceLogEl);
  }

  function startTracePolling(turnId, label) {
    traceDivider(`▶ ${label}  (turn ${shortId(turnId)})`);
    traceStatusEl.textContent = 'live';
    traceStatusEl.classList.add('live');

    let lastStep = 0;
    let stopped = false;

    async function poll() {
      try {
        const res = await fetch(`/trace/${state.conversationId}?turn_id=${turnId}`);
        if (!res.ok) return;
        const data = await res.json();
        for (const ev of data.trace) {
          if (ev.step > lastStep) {
            appendTraceLine(ev);
            lastStep = ev.step;
          }
        }
      } catch (err) {
        // transient network hiccup while polling — next tick retries
      }
    }

    const interval = setInterval(poll, 300);
    poll();

    return async function stop() {
      if (stopped) return;
      stopped = true;
      clearInterval(interval);
      await poll(); // catch any trailing steps written just before the main call returned
      traceStatusEl.textContent = 'idle';
      traceStatusEl.classList.remove('live');
    };
  }

  // ---------------- actions ----------------

  function setBusy(busy) {
    state.busy = busy;
    input.disabled = busy;
    sendBtn.disabled = busy;
    newConvBtn.disabled = busy;
  }

  async function sendMessage(text) {
    setBusy(true);
    appendUserMessage(text);
    showThinking();

    const turnId = newId();
    const label = `Sending: "${text.length > 40 ? text.slice(0, 40) + '…' : text}"`;
    const stopPolling = startTracePolling(turnId, label);

    try {
      const res = await fetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation_id: state.conversationId, turn_id: turnId, message: text }),
      });
      const data = await res.json();
      hideThinking();
      if (!res.ok) {
        appendSystemMessage('Error: ' + (data.detail || res.status));
      } else {
        appendAssistantMessage(data);
      }
    } catch (err) {
      hideThinking();
      appendSystemMessage('Request failed: ' + err.message);
    } finally {
      await stopPolling();
      setBusy(false);
      input.focus();
    }
  }

  async function raiseTicket(messageId, note) {
    const turnId = newId();
    const stopPolling = startTracePolling(turnId, 'Raising ticket');
    try {
      const res = await fetch('/ticket', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          conversation_id: state.conversationId,
          turn_id: turnId,
          message_id: messageId,
          note: note || undefined,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        appendSystemMessage('Ticket request failed: ' + (data.detail || res.status));
      } else {
        appendSystemMessage(
          `🎫 Ticket #${data.ticket_id} raised · ${String(data.priority).toUpperCase()} priority · ${data.subject}\n${data.customer_reply}`
        );
      }
    } catch (err) {
      appendSystemMessage('Ticket request failed: ' + err.message);
    } finally {
      await stopPolling();
    }
  }

  // ---------------- setup ----------------

  function newConversation() {
    state.conversationId = newId();
    state.fieldHistory = null;
    hideHistoryDropdown();
    messagesEl.innerHTML = '';
    traceLogEl.innerHTML = '';
    convBadge.textContent = 'conv: ' + shortId(state.conversationId);
  }

  async function loadHealth() {
    try {
      const res = await fetch('/health');
      const data = await res.json();
      llmBadge.textContent = 'LLM: ' + data.llm_mode;
    } catch (err) {
      llmBadge.textContent = 'LLM: unknown';
    }
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || state.busy) return;
    input.value = '';
    sendMessage(text);
  });

  newConvBtn.addEventListener('click', () => {
    if (state.busy) return;
    newConversation();
  });

  input.addEventListener('focus', showHistoryDropdown);
  input.addEventListener('blur', hideHistoryDropdown);
  // Once the customer starts typing their own answer, a stale history list
  // sitting over the input is just clutter, not help.
  input.addEventListener('input', hideHistoryDropdown);

  loadHealth();
  newConversation();
})();
