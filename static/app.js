/* Nexus client — vanilla JS, no build step. */
'use strict';

// ------------------------------------------------------------------ state

const state = {
  me: null,
  guilds: [],
  dms: [],
  friends: { friends: [], incoming: [], outgoing: [] },
  members: [],
  activeGuildId: null,      // null === home (DMs + friends)
  activeChannelId: null,
  activeChannel: null,      // channel info object
  messages: [],
  replyTo: null,            // message being replied to, if any
  mentionable: [],          // members of the open channel, for @ autocomplete
  wallet: null,             // Sana Coin balance, stats and claim cooldown
  rev: '',
  channelRev: '',           // fingerprint of the open channel (edits/reactions)
  homeTab: 'friends',       // 'friends' when no DM is open
  pollAbort: null,
  atBottom: true,
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

// ------------------------------------------------------------------ api

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
    credentials: 'same-origin',
  });
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function toast(message, kind = '') {
  const node = el('div', `toast ${kind}`, message);
  $('#toasts').appendChild(node);
  setTimeout(() => {
    node.style.transition = 'opacity .2s';
    node.style.opacity = '0';
    setTimeout(() => node.remove(), 220);
  }, 2600);
}

// ------------------------------------------------------------------ utils

function escapeHtml(str) {
  return str.replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function unescapeHtml(str) {
  return str.replace(/&amp;|&lt;|&gt;|&quot;|&#39;/g, (c) => (
    { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'" }[c]
  ));
}

// Mentions are stored as the full tag so they survive a rename ambiguity;
// they're displayed as a plain @name pill.
const MENTION_RE = /@([A-Za-z0-9_.\- ]{2,32})#(\d{4})/g;

function formatContent(raw) {
  let html = escapeHtml(raw);
  html = html.replace(/`([^`\n]+)`/g, (_, code) => `<code>${code}</code>`);
  html = html.replace(MENTION_RE, (full, name, disc) => {
    const tag = `${name}#${disc}`;
    const isMe = state.me && tag === state.me.tag;
    return `<span class="mention${isMe ? ' me' : ''}" title="${escapeHtml(tag)}">`
      + `@${name}</span>`;
  });
  html = html.replace(/@everyone\b/g, '<span class="mention everyone">@everyone</span>');
  html = html.replace(/https?:\/\/[^\s<]+[^\s<.,:;"')\]]/g, (url) => {
    const href = unescapeHtml(url);
    return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${url}</a>`;
  });
  return html;
}

/** Plain-text version for reply previews: strip the #0000 off mentions. */
function previewText(raw) {
  return (raw || '').replace(MENTION_RE, (_, name) => `@${name}`);
}

function initials(name) {
  return (name || '?').trim().charAt(0).toUpperCase();
}

function avatar(user, size = '') {
  const node = el('div', `avatar ${size}`.trim());
  if (user.avatarUrl) {
    const img = el('img');
    img.src = user.avatarUrl;
    img.alt = '';
    // If the file ever goes missing, fall back to the coloured initial.
    img.onerror = () => {
      img.remove();
      node.textContent = initials(user.username || user.name);
      node.style.background = user.color || '#5865f2';
    };
    node.appendChild(img);
  } else {
    node.textContent = initials(user.username || user.name);
    node.style.background = user.color || '#5865f2';
  }
  if (user.online !== undefined) node.dataset.status = user.online ? 'online' : 'offline';
  if (hasPerk(user, 'ring')) node.classList.add('ringed');
  // Decorations sit on top of the avatar. Skipped at xs, where a hat on a
  // 16px circle is a smudge rather than a joke.
  if (user.decoration === 'fedora' && size !== 'xs') node.appendChild(fedoraNode());
  return node;
}

/** The fedora, drawn rather than uploaded so it stays sharp at every size. */
function fedoraNode() {
  const hat = document.createElement('div');
  hat.className = 'decoration fedora';
  hat.title = 'Fedora';
  hat.innerHTML = `
    <svg viewBox="0 0 100 52" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <ellipse cx="50" cy="40" rx="47" ry="9" fill="#1b1c20"/>
      <ellipse cx="50" cy="38" rx="47" ry="9" fill="#31333b"/>
      <path d="M30 39 C29 20 33 8 50 8 C67 8 71 20 70 39 Z" fill="#3c3f49"/>
      <path d="M50 8 C40 8 35 14 33 22 C38 16 44 14 50 14 Z" fill="#4a4e5a"/>
      <path d="M29 30 H71 V38 H29 Z" fill="#c8a24a"/>
      <path d="M29 30 H71 V33 H29 Z" fill="#dcb75f"/>
    </svg>`;
  return hat;
}

function hasPerk(user, id) {
  return !!(user && user.perks && user.perks.includes(id));
}

/** A username, plus whatever the shop has bolted onto it. */
function nameNode(user, cls = '') {
  const wrap = el('span', `name ${cls}`.trim());
  const text = el('span', 'name-text', user ? (user.username || user.name) : '—');
  if (hasPerk(user, 'glow')) text.classList.add('glow');
  wrap.appendChild(text);
  if (hasPerk(user, 'badge')) {
    const vip = el('span', 'vip-tag', 'VIP');
    vip.title = 'VIP — bought from the Frontman shop';
    wrap.appendChild(vip);
  }
  return wrap;
}

const DAY_MS = 86400000;

function timeLabel(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

function dayLabel(ts) {
  const d = new Date(ts * 1000);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const that = new Date(d); that.setHours(0, 0, 0, 0);
  const diff = Math.round((today - that) / DAY_MS);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  return d.toLocaleDateString([], { month: 'long', day: 'numeric', year: 'numeric' });
}

function stampLabel(ts) {
  const d = new Date(ts * 1000);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const that = new Date(d); that.setHours(0, 0, 0, 0);
  const diff = Math.round((today - that) / DAY_MS);
  if (diff === 0) return `Today at ${timeLabel(ts)}`;
  if (diff === 1) return `Yesterday at ${timeLabel(ts)}`;
  return `${d.toLocaleDateString()} ${timeLabel(ts)}`;
}

// ------------------------------------------------------------------ modal

function openModal(build) {
  const box = $('#modal-content');
  box.replaceChildren();
  build(box, closeModal);
  $('#overlay').classList.remove('hidden');
  const focusable = box.querySelector('input, textarea, button');
  if (focusable) setTimeout(() => focusable.focus(), 30);
}

function closeModal() {
  $('#overlay').classList.add('hidden');
  $('#modal-content').replaceChildren();
}

$('#modal-close').onclick = closeModal;
$('#overlay').onclick = (e) => { if (e.target.id === 'overlay') closeModal(); };
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('#overlay').classList.contains('hidden')) closeModal();
});

function confirmModal(title, message, confirmLabel, onConfirm, danger = true) {
  openModal((box, close) => {
    box.appendChild(el('h2', null, title));
    box.appendChild(el('p', 'sub', message));
    const actions = el('div', 'modal-actions');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = close;
    const go = el('button', `btn ${danger ? 'danger' : 'primary'}`, confirmLabel);
    go.onclick = async () => {
      go.disabled = true;
      try { await onConfirm(); close(); }
      catch (err) { toast(err.message, 'error'); go.disabled = false; }
    };
    actions.append(cancel, go);
    box.appendChild(actions);
  });
}

// ------------------------------------------------------------------ auth UI

let authMode = 'login';

function setAuthMode(mode) {
  authMode = mode;
  const registering = mode === 'register';
  document.querySelectorAll('.tab').forEach((t) =>
    t.classList.toggle('active', t.dataset.mode === mode));
  $('#field-username').classList.toggle('hidden', !registering);
  $('#field-fullname').classList.toggle('hidden', !registering);
  $('#auth-form').username.required = registering;
  $('#auth-form').fullName.required = registering;
  $('#auth-submit').textContent = registering ? 'Create account' : 'Sign in';
  $('#auth-form').password.autocomplete = registering ? 'new-password' : 'current-password';
  $('#auth-switch-text').textContent = registering ? 'Already registered?' : 'Need an account?';
  $('#auth-switch-btn').textContent = registering ? 'Sign in' : 'Create one';
  $('#auth-error').textContent = '';
}

document.querySelectorAll('.tab').forEach((t) => {
  t.onclick = () => setAuthMode(t.dataset.mode);
});
$('#auth-switch-btn').onclick = () =>
  setAuthMode(authMode === 'login' ? 'register' : 'login');

$('#auth-form').onsubmit = async (e) => {
  e.preventDefault();
  const form = e.target;
  const btn = $('#auth-submit');
  $('#auth-error').textContent = '';
  btn.disabled = true;
  try {
    const payload = {
      email: form.email.value,
      password: form.password.value,
    };
    if (authMode === 'register') {
      payload.username = form.username.value;
      payload.fullName = form.fullName.value;
    }
    const data = await api('POST', `/api/${authMode}`, payload);
    state.me = data.user;
    form.reset();
    // A new account is held until an administrator lets it in.
    if (!data.user.approved) showPending();
    else await enterApp();
  } catch (err) {
    $('#auth-error').textContent = err.message;
  } finally {
    btn.disabled = false;
  }
};

// ------------------------------------------------------------------ render: rail

function guildInitials(name) {
  return name.split(/\s+/).slice(0, 2).map((w) => w[0]).join('').toUpperCase();
}

function renderRail() {
  const list = $('#rail-guilds');
  list.replaceChildren();

  const dmUnread = state.dms.reduce((n, d) => n + d.unread, 0)
    + state.friends.incoming.length;
  const home = $('#rail-home');
  home.classList.toggle('active', state.activeGuildId === null);
  home.querySelector('.rail-badge')?.remove();
  if (dmUnread > 0) {
    home.appendChild(el('span', 'rail-badge', dmUnread > 99 ? '99+' : String(dmUnread)));
  }

  state.guilds.forEach((g) => {
    const btn = el('button', 'rail-btn guild');
    btn.title = g.name;
    if (g.iconUrl) {
      const img = el('img', 'rail-icon');
      img.src = g.iconUrl;
      img.alt = '';
      btn.appendChild(img);
      btn.style.background = 'var(--bg-3)';
    } else {
      btn.style.background = state.activeGuildId === g.id ? g.color : 'var(--bg-3)';
      btn.textContent = guildInitials(g.name);
    }
    btn.classList.toggle('active', state.activeGuildId === g.id);
    // Muted channels are deliberately invisible from out here too.
    const loud = g.channels.filter((c) => !c.muted);
    const unread = loud.reduce((n, c) => n + c.unread, 0);
    const pings = loud.reduce((n, c) => n + (c.mentions || 0), 0);
    if (pings > 0) {
      const b = el('span', 'rail-badge ping', pings > 99 ? '99+' : `@${pings}`);
      b.title = `${pings} message${pings === 1 ? '' : 's'} mentioning you`;
      btn.appendChild(b);
      if (state.activeGuildId !== g.id) btn.classList.add('pinged');
    } else if (unread > 0) {
      btn.appendChild(el('span', 'rail-badge', unread > 99 ? '99+' : String(unread)));
      if (state.activeGuildId !== g.id) btn.classList.add('pinged');
    }
    if (!g.iconUrl) {
      btn.onmouseenter = () => { btn.style.background = g.color; };
      btn.onmouseleave = () => {
        if (state.activeGuildId !== g.id) btn.style.background = 'var(--bg-3)';
      };
    }
    btn.onclick = () => openGuild(g.id);
    list.appendChild(btn);
  });
}

// ------------------------------------------------------------------ render: sidebar

function renderSidebar() {
  const body = $('#sidebar-body');
  body.replaceChildren();
  const guild = state.guilds.find((g) => g.id === state.activeGuildId);

  if (guild) {
    $('#sidebar-title').textContent = guild.name;
    $('#guild-menu-btn').hidden = false;

    const head = el('div', 'side-section');
    head.appendChild(el('span', null, 'Text channels'));
    if (guild.isOwner) {
      const add = el('button', null, '+');
      add.title = 'Create channel';
      add.onclick = () => promptCreateChannel(guild.id);
      head.appendChild(add);
    }
    body.appendChild(head);

    guild.channels.forEach((c) => {
      const item = el('button', 'side-item');
      item.dataset.channelId = String(c.id);
      // The lounge is bought, not arranged: leave it pinned at the bottom.
      if (guild.isOwner && !c.locked) {
        item.draggable = true;
        item.classList.add('draggable');
        item.title = 'Drag to reorder';
      }
      item.classList.toggle('active', c.id === state.activeChannelId);
      if (c.unread > 0 && !c.muted) item.classList.add('unread');
      if (c.muted) item.classList.add('muted');
      item.appendChild(el('span', 'hash', c.locked ? '🔒' : '#'));
      item.appendChild(el('span', 'label', c.name));
      // A muted channel keeps its unread count; it just stops shouting it.
      if (!c.muted && c.mentions > 0 && c.id !== state.activeChannelId) {
        const ping = el('span', 'badge ping', c.mentions > 99 ? '99+' : `@${c.mentions}`);
        ping.title = `${c.mentions} message${c.mentions === 1 ? '' : 's'} mentioning you`;
        item.appendChild(ping);
      } else if (!c.muted && c.unread > 0 && c.id !== state.activeChannelId) {
        item.appendChild(el('span', 'badge', c.unread > 99 ? '99+' : String(c.unread)));
      }
      item.onclick = () => openChannel(c.id);

      const mute = el('span', `mute-btn ${c.muted ? 'on' : ''}`.trim(),
        c.muted ? '🔕' : '🔔');
      mute.title = c.muted ? `Unmute #${c.name}` : `Mute #${c.name}`;
      mute.onclick = (e) => {
        e.stopPropagation();
        toggleMute(c);
      };
      item.appendChild(mute);

      if (guild.isOwner && guild.channels.length > 1 && !c.locked) {
        const x = el('span', 'x', '×');
        x.title = 'Delete channel';
        x.onclick = (e) => {
          e.stopPropagation();
          confirmModal('Delete channel?',
            `#${c.name} and all of its messages will be permanently removed.`,
            'Delete', async () => {
              await api('DELETE', `/api/channels/${c.id}`);
              if (state.activeChannelId === c.id) state.activeChannelId = null;
              await refreshAll();
            });
        };
        item.appendChild(x);
      }
      body.appendChild(item);
    });
    if (guild.isOwner) enableChannelDrag(body, guild);
    return;
  }

  // ---- home: friends + DM list
  $('#sidebar-title').textContent = 'Direct Messages';
  $('#guild-menu-btn').hidden = true;

  const friendsBtn = el('button', 'side-item');
  friendsBtn.classList.toggle('active', state.activeChannelId === null);
  friendsBtn.appendChild(el('span', 'hash', '☺'));
  friendsBtn.appendChild(el('span', 'label', 'Friends'));
  if (state.friends.incoming.length) {
    friendsBtn.appendChild(el('span', 'badge', String(state.friends.incoming.length)));
  }
  friendsBtn.onclick = () => openFriends();
  body.appendChild(friendsBtn);

  const head = el('div', 'side-section');
  head.appendChild(el('span', null, 'Direct messages'));
  const add = el('button', null, '+');
  add.title = 'Start a DM';
  add.onclick = () => openFriends();
  head.appendChild(add);
  body.appendChild(head);

  if (!state.dms.length) {
    const hint = el('div', 'muted');
    hint.style.padding = '4px 8px';
    hint.textContent = 'No conversations yet. Add a friend to start one.';
    body.appendChild(hint);
  }

  state.dms.forEach((d) => {
    const item = el('button', 'side-item');
    item.classList.toggle('active', d.channelId === state.activeChannelId);
    if (d.unread > 0) item.classList.add('unread');
    item.appendChild(avatar(d.user));
    item.appendChild(el('span', 'label', d.user.username));
    if (d.unread > 0 && d.channelId !== state.activeChannelId) {
      item.appendChild(el('span', 'badge', d.unread > 99 ? '99+' : String(d.unread)));
    }
    item.onclick = () => openChannel(d.channelId);
    body.appendChild(item);
  });
}

/** Silence or unsilence a channel, redrawing straight away. */
async function toggleMute(channel) {
  const wanted = !channel.muted;
  channel.muted = wanted;                    // optimistic: the click feels instant
  renderSidebar();
  renderRail();
  try {
    const data = await api('POST', `/api/channels/${channel.id}/mute`, { muted: wanted });
    channel.muted = data.muted;
  } catch (err) {
    toast(err.message, 'error');
    channel.muted = !wanted;
  }
  renderSidebar();
  renderRail();
}

/** Let the owner drag channels into a new order in the sidebar. */
function enableChannelDrag(container, guild) {
  let dragging = null;

  const items = () => [...container.querySelectorAll('.side-item.draggable')];

  container.querySelectorAll('.side-item.draggable').forEach((item) => {
    item.addEventListener('dragstart', (e) => {
      dragging = item;
      item.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      // Firefox refuses to start a drag unless some data is set.
      e.dataTransfer.setData('text/plain', item.dataset.channelId);
    });

    item.addEventListener('dragend', async () => {
      item.classList.remove('dragging');
      if (!dragging) return;
      dragging = null;

      // Re-read the guild from state: a background refresh may have replaced
      // the object captured when this row was rendered, and comparing against
      // that stale copy would send the wrong order.
      const live = state.guilds.find((g) => g.id === guild.id);
      if (!live) return;

      const order = items().map((n) => Number(n.dataset.channelId));
      const current = live.channels.map((c) => c.id);
      if (order.join() === current.join()) return;      // nothing moved

      // Show the new order straight away; put it back if the server refuses.
      const byId = new Map(live.channels.map((c) => [c.id, c]));
      if (order.some((id) => !byId.has(id))) return;    // list moved on
      live.channels = order.map((id) => byId.get(id));
      try {
        await api('PATCH', `/api/guilds/${live.id}/channels/order`, { order });
      } catch (err) {
        toast(err.message, 'error');
        live.channels = current.map((id) => byId.get(id));
        renderSidebar();
      }
    });
  });

  // The sidebar element outlives each render — only its children are replaced —
  // so these must be attached once or they pile up on every redraw.
  if (!container.dataset.dragWired) {
    container.dataset.dragWired = '1';
    container.addEventListener('dragover', (e) => {
      const held = container.querySelector('.side-item.dragging');
      if (!held) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const after = dropTargetFor(container, held, e.clientY);
      if (after === null) container.appendChild(held);
      else container.insertBefore(held, after);
    });
    container.addEventListener('drop', (e) => {
      if (container.querySelector('.side-item.dragging')) e.preventDefault();
    });
  }

}

/** The item the dragged one should sit before, or null for the end. */
function dropTargetFor(container, held, y) {
  const others = [...container.querySelectorAll('.side-item.draggable')]
    .filter((n) => n !== held);
  for (const node of others) {
    const rect = node.getBoundingClientRect();
    if (y < rect.top + rect.height / 2) return node;
  }
  return null;
}

// ------------------------------------------------------------------ render: members

async function renderMembers() {
  const panel = $('#members-panel');
  if (!state.activeGuildId || !state.activeChannel || state.activeChannel.kind !== 'text') {
    panel.hidden = true;
    $('#members-btn').hidden = true;
    return;
  }
  $('#members-btn').hidden = false;
  try {
    const data = await api('GET', `/api/guilds/${state.activeGuildId}/members`);
    state.members = data.members;
  } catch { return; }

  panel.hidden = false;
  $('#member-count').textContent = state.members.length;
  const list = $('#members-list');
  list.replaceChildren();
  state.members.forEach((m) => {
    const row = el('button', `member ${m.online ? '' : 'offline'}`.trim());
    row.appendChild(avatar(m));
    // Name and status stack, so a status never widens the row.
    const text = el('div', 'm-text');
    const line = el('div', 'm-line');
    line.appendChild(nameNode(m, 'm-name'));
    if (m.isBot) line.appendChild(el('span', 'bot-tag', 'BOT'));
    if (m.isOwner) {
      const crown = el('span', 'crown', '♛');
      crown.title = 'Server owner';
      line.appendChild(crown);
    }
    text.appendChild(line);
    if (m.status) {
      const st = el('div', 'm-status', m.status);
      st.title = m.status;
      text.appendChild(st);
    }
    row.appendChild(text);
    row.onclick = () => (m.isBot ? showBotPanel() : showProfile(m.id));
    list.appendChild(row);
  });
}

// ------------------------------------------------------------------ render: messages

function renderMessages() {
  const box = $('#messages');
  const wasAtBottom = state.atBottom;
  // The reactor card lives on <body>, so a redraw would strand it pointing at
  // a pill that no longer exists.
  hideReactors();
  box.replaceChildren();

  if (!state.messages.length) {
    box.appendChild(emptyChannelNotice());
    return;
  }

  let prev = null;
  state.messages.forEach((m) => {
    const newDay = !prev || dayLabel(prev.createdAt) !== dayLabel(m.createdAt);
    if (newDay) box.appendChild(el('div', 'divider', dayLabel(m.createdAt)));

    const grouped = !newDay && prev
      && prev.author.id === m.author.id
      && m.createdAt - prev.createdAt < 420;

    box.appendChild(messageNode(m, grouped));
    prev = m;
  });

  if (wasAtBottom) scrollToBottom();
}

function messageNode(m, grouped) {
  // Replies and cards always show their own header.
  if (m.replyToId || m.game || m.poll || m.signup) grouped = false;
  const node = el('div', `msg ${grouped ? 'grouped' : ''}`.trim());
  node.dataset.id = m.id;
  if (m.mentionsMe) node.classList.add('pinged');

  if (m.replyToId) node.appendChild(replyPreview(m));

  const gutter = el('div', 'msg-gutter');
  if (grouped) {
    gutter.appendChild(el('span', 'msg-time-hover', timeLabel(m.createdAt)));
  } else {
    gutter.appendChild(avatar(m.author, 'lg'));
  }
  node.appendChild(gutter);

  const body = el('div', 'msg-body');
  if (!grouped) {
    const head = el('div', 'msg-head');
    const author = nameNode(m.author, 'msg-author');
    author.style.color = m.author.color;
    author.onclick = () => showProfile(m.author.id);
    head.appendChild(author);
    if (m.author.isBot) head.appendChild(el('span', 'bot-tag', 'BOT'));
    head.appendChild(el('span', 'msg-time', stampLabel(m.createdAt)));
    body.appendChild(head);
  }
  const gifUrl = loneGifUrl(m.content);
  const text = el('div', 'msg-text');
  text.innerHTML = formatContent(m.content);
  if (m.editedAt) {
    const tag = el('span', 'edited', '(edited)');
    tag.title = stampLabel(m.editedAt);
    text.appendChild(tag);
  }
  if (m.content && !gifUrl) body.appendChild(text);
  if (gifUrl) body.appendChild(gifNode(gifUrl));

  if (m.game) body.appendChild(gameNode(m));
  if (m.signup) body.appendChild(signupNode(m));
  if (m.poll) body.appendChild(pollNode(m));
  if (m.sticker) body.appendChild(stickerNode(m.sticker));
  (m.attachments || []).forEach((a) => body.appendChild(attachmentNode(a)));
  if (m.reactions && m.reactions.length) body.appendChild(reactionBar(m));

  node.appendChild(body);

  const mine = m.author.id === state.me.id;
  const guild = state.guilds.find((g) => g.id === state.activeGuildId);
  const canDelete = mine || (guild && guild.isOwner);
  {
    const actions = el('div', 'msg-actions');
    const react = el('button', null, '☺');
    react.title = 'Add reaction';
    react.onclick = (e) => openEmojiPicker(e.currentTarget, (emoji) => react_(m.id, emoji));
    actions.appendChild(react);
    const reply = el('button', null, '↩');
    reply.title = 'Reply';
    reply.onclick = () => startReply(m);
    actions.appendChild(reply);
    if (mine && m.content && !m.sticker) {
      const edit = el('button', null, '✎');
      edit.title = 'Edit';
      edit.onclick = () => startEdit(m, body, text);
      actions.appendChild(edit);
    }
    if (canDelete) {
      const del = el('button', null, '🗑');
      del.title = 'Delete';
      del.onclick = () => confirmModal('Delete message?',
        'This cannot be undone.', 'Delete', async () => {
          await api('DELETE', `/api/messages/${m.id}`);
          state.messages = state.messages.filter((x) => x.id !== m.id);
          renderMessages();
        });
      actions.appendChild(del);
    }
    node.appendChild(actions);
  }
  return node;
}

// ------------------------------------------------------------------ replies

function replyPreview(m) {
  const bar = el('div', 'reply-preview');
  bar.appendChild(el('span', 'reply-spine'));

  if (!m.replyTo) {
    bar.appendChild(el('span', 'reply-gone', 'Original message was deleted'));
    return bar;
  }

  const who = el('span', 'reply-author', m.replyTo.author.username);
  who.style.color = m.replyTo.author.color;
  who.onclick = () => showProfile(m.replyTo.author.id);

  bar.appendChild(avatar(m.replyTo.author, 'xs'));
  bar.appendChild(who);
  bar.appendChild(el('span', 'reply-text', previewText(m.replyTo.content) || 'Click to see'));
  bar.onclick = (e) => {
    if (e.target === who) return;
    jumpToMessage(m.replyTo.id);
  };
  return bar;
}

function jumpToMessage(id) {
  const target = $(`#messages .msg[data-id="${id}"]`);
  if (!target) {
    toast('That message is further up than the last 50 — scroll up to find it.');
    return;
  }
  target.scrollIntoView({ block: 'center', behavior: 'smooth' });
  target.classList.remove('flash');
  void target.offsetWidth;            // restart the animation if it's re-clicked
  target.classList.add('flash');
  setTimeout(() => target.classList.remove('flash'), 1600);
}

function startReply(m) {
  state.replyTo = m;
  const label = m.author.username;
  $('#reply-bar-name').textContent = label;
  $('#reply-bar').hidden = false;
  input.focus();
}

function cancelReply() {
  state.replyTo = null;
  $('#reply-bar').hidden = true;
}

$('#reply-cancel').onclick = cancelReply;

// ------------------------------------------------------------------- GIFs

// A message that is nothing but a Giphy link renders as the GIF itself.
// Restricted to Giphy's own media hosts so an arbitrary link can't be turned
// into an inline image by anyone who works out the format.
const GIF_URL_RE = /^https:\/\/(?:[a-z0-9-]+\.)?giphy\.com\/media\/[^\s]+\.gif$/i;

function loneGifUrl(content) {
  const trimmed = (content || '').trim();
  return GIF_URL_RE.test(trimmed) ? trimmed : null;
}

function gifNode(url) {
  const wrap = el('div', 'attachment gif');
  const img = el('img');
  img.src = url;
  img.alt = 'GIF';
  img.loading = 'lazy';
  img.onclick = () => openLightbox({ url, name: 'GIF', size: 0 });
  wrap.appendChild(img);
  return wrap;
}

// ------------------------------------------------------------ blackjack card

const RED_SUITS = ['♥', '♦'];

function cardNode(card) {
  if (card === '??') {
    const back = el('div', 'card back');
    back.appendChild(el('span', 'card-mark', '★'));
    return back;
  }
  const suit = card.slice(-1);
  const rank = card.slice(0, -1);
  const node = el('div', `card ${RED_SUITS.includes(suit) ? 'red' : ''}`.trim());
  node.appendChild(el('span', 'card-tl', `${rank}${suit}`));
  node.appendChild(el('span', 'card-mid', suit));
  node.appendChild(el('span', 'card-br', `${rank}${suit}`));
  return node;
}

function seatRow(seat, label, isTurn) {
  const wrap = el('div', `seat ${isTurn ? 'active' : ''}`.trim());
  const split = !!(seat.hands && seat.hands.length > 1);

  const head = el('div', 'seat-head');
  if (seat.user) head.appendChild(avatar({ ...seat.user, online: undefined }, 'xs'));
  head.appendChild(seat.user ? nameNode(seat.user, 'seat-name')
                             : el('span', 'seat-name', label));
  // A split seat has no single total — each hand carries its own.
  if (split) {
    head.appendChild(el('span', 'seat-flag', `${seat.hands.length} HANDS`));
  } else if (seat.total !== null) {
    const total = el('span', 'seat-total', String(seat.total));
    if (seat.busted) total.classList.add('bust');
    head.appendChild(total);
  } else {
    head.appendChild(el('span', 'seat-total hidden-total', '?'));
  }
  if (!split && seat.busted) head.appendChild(el('span', 'seat-flag bust-flag', 'BUST'));
  else if (!split && seat.stood) head.appendChild(el('span', 'seat-flag', 'STAND'));
  wrap.appendChild(head);

  // One row per hand once a split is in play; a lone hand needs no label.
  if (split) {
    seat.hands.forEach((h, i) => wrap.appendChild(splitHandNode(h, i)));
    return wrap;
  }

  const hand = el('div', 'hand');
  seat.cards.forEach((c) => hand.appendChild(cardNode(c)));
  wrap.appendChild(hand);
  if (seat.hands && seat.hands[0] && seat.hands[0].doubled) {
    wrap.appendChild(el('span', 'seat-flag', 'DOUBLED'));
  }
  return wrap;
}

/** "Balance: 12,340" — what the viewer is holding, live, beside the stake. */
function balanceChip(g) {
  const chip = el('span', 'game-balance');
  chip.appendChild(el('span', 'gb-label', 'Balance:'));
  chip.appendChild(el('span', 'gb-value', Number(g.balance || 0).toLocaleString()));
  chip.appendChild(el('span', null, COIN));
  chip.title = 'What you are holding right now';
  return chip;
}

const SPLIT_OUTCOME = { host: 'won', opp: 'lost', push: 'push' };

function splitHandNode(h, index) {
  const box = el('div', `split-hand ${h.active ? 'active' : ''}`.trim());

  const head = el('div', 'split-head');
  head.appendChild(el('span', 'split-label', `Hand ${index + 1}`));
  const total = el('span', 'seat-total', String(h.total));
  if (h.busted) total.classList.add('bust');
  head.appendChild(total);
  if (h.doubled) head.appendChild(el('span', 'seat-flag', 'DOUBLED'));
  else if (h.busted) head.appendChild(el('span', 'seat-flag bust-flag', 'BUST'));
  else if (h.stood) head.appendChild(el('span', 'seat-flag', 'STAND'));
  if (h.result) {
    const word = SPLIT_OUTCOME[h.result.winner] || '';
    head.appendChild(el('span', `split-outcome ${h.result.winner}`, word));
  }
  if (h.stake) head.appendChild(el('span', 'split-stake', coins(h.stake)));
  box.appendChild(head);

  const hand = el('div', 'hand');
  h.cards.forEach((c) => hand.appendChild(cardNode(c)));
  box.appendChild(hand);
  return box;
}

function slotsNode(g) {
  const card = el('div', `game-card slots ${g.profit > 0 ? 'won' : ''}`.trim());
  const head = el('div', 'game-head');
  head.appendChild(el('span', 'game-title', 'Slot machine'));
  head.appendChild(el('span', 'game-stake', g.bet ? coins(g.bet) : 'for fun'));
  head.appendChild(balanceChip(g));
  card.appendChild(head);

  const reels = el('div', 'reels');
  g.reels.forEach((sym) => reels.appendChild(el('div', 'reel', sym)));
  card.appendChild(reels);

  const outcome = g.profit > 0 ? 'win' : (g.profit === 0 ? 'push' : 'lose');
  const banner = el('div', `game-result ${outcome}`);
  banner.appendChild(el('strong', null, g.profit > 0
    ? `+${g.payout.toLocaleString()} ${COIN}`
    : (g.profit === 0 ? 'Stake returned' : `-${g.bet.toLocaleString()} ${COIN}`)));
  banner.appendChild(el('span', null, g.label));
  card.appendChild(banner);

  if (g.yourSeat) {
    const actions = el('div', 'game-actions');
    const again = el('button', 'btn small ghost', 'Spin again');
    again.onclick = () => spinSlots(g.bet || 10, g.messageId);
    actions.appendChild(again);
    card.appendChild(actions);
  }

  const foot = el('div', 'game-foot');
  foot.textContent = `${g.player ? g.player.username : 'someone'}'s spin`;
  card.appendChild(foot);
  return card;
}

const STAGE_LABEL = {
  waiting: 'Waiting for players',
  flop: 'Flop',
  turn: 'Turn',
  river: 'River',
  showdown: 'Showdown',
};

function pokerNode(g) {
  const card = el('div', `game-card poker ${g.stage}`.trim());

  const head = el('div', 'game-head');
  head.appendChild(el('span', 'game-title', "Poker · Hold'em"));
  if (g.ante) head.appendChild(el('span', 'game-stake', coins(g.ante)));
  head.appendChild(balanceChip(g));
  const status = `${STAGE_LABEL[g.stage]} · pot ${g.pot.toLocaleString()} ${COIN}`;
  head.appendChild(el('span', 'game-status',
    g.toMatch ? `${status} · ${g.toMatch.toLocaleString()} to match` : status));
  card.appendChild(head);

  if (g.board.length) {
    const boardRow = el('div', 'poker-board');
    g.board.forEach((c) => boardRow.appendChild(cardNode(c)));
    card.appendChild(boardRow);
  }

  const seats = el('div', 'poker-seats');
  g.seats.forEach((s) => {
    const row = el('div', 'poker-seat');
    if (s.folded) row.classList.add('folded');
    if (s.won) row.classList.add('winner');
    if (s.isYou) row.classList.add('you');

    const who = el('div', 'poker-who');
    if (s.user) who.appendChild(avatar({ ...s.user, online: undefined }, 'xs'));
    who.appendChild(nameNode(s.user, 'poker-name'));
    if (s.folded) who.appendChild(el('span', 'seat-flag', 'FOLD'));
    else if (s.allIn) who.appendChild(el('span', 'seat-flag all-in-flag', 'ALL IN'));
    else if (s.acted && g.stage !== 'showdown') who.appendChild(el('span', 'seat-flag', 'IN'));
    if (s.won) who.appendChild(el('span', 'seat-flag win-flag', 'WON'));
    row.appendChild(who);

    const hand = el('div', 'poker-hole');
    s.hole.forEach((c) => hand.appendChild(cardNode(c)));
    row.appendChild(hand);

    // What they have put in this street, so a raise is visible around the table.
    if (s.street > 0 && g.stage !== 'showdown') {
      row.appendChild(el('span', 'poker-chips', `${s.street.toLocaleString()} ${COIN}`));
    }

    if (s.hand) row.appendChild(el('span', 'poker-hand-name', s.hand.name));
    seats.appendChild(row);
  });
  card.appendChild(seats);

  if (g.result) {
    const mine = g.seats.find((s) => s.isYou);
    const iWon = !!(mine && mine.won);
    const banner = el('div', `game-result ${iWon ? 'win' : (g.youArePlaying ? 'lose' : '')}`.trim());
    // Side pots mean the winners can take different amounts, so each is named
    // with what they actually got rather than one figure for everybody.
    const took = g.seats.filter((s) => s.won).map((s) =>
      `${s.user ? s.user.username : '?'} ${(s.wonAmount || 0).toLocaleString()} ${COIN}`);
    banner.appendChild(el('strong', null, iWon
      ? `You won ${(mine.wonAmount || 0).toLocaleString()} ${COIN}`
      : `${took.join(' · ')}`));
    banner.appendChild(el('span', null, g.result.text));
    card.appendChild(banner);
  }

  const actions = el('div', 'game-actions');
  if (g.canJoin) {
    const join = el('button', 'btn primary small', `Sit down for ${coins(g.ante)}`);
    join.onclick = () => pokerCall(g.id, 'join');
    actions.appendChild(join);
  }
  if (g.canDeal) {
    const deal = el('button', 'btn primary small', 'Deal');
    deal.onclick = () => pokerCall(g.id, 'deal');
    actions.appendChild(deal);
  }
  if (g.yourTurn) {
    // Checking is free, so it is the safe default when nothing is owed; when
    // there is, the same slot becomes Call and says what it costs.
    // Short of the call, the same button puts the rest in and goes all in.
    const short = g.toCall > g.balance;
    const stay = el('button', 'btn primary small',
      g.toCall ? (short ? `All in for ${coins(g.balance)}` : `Call ${coins(g.toCall)}`)
               : 'Check');
    stay.onclick = () => pokerCall(g.id, 'action',
      { action: g.toCall ? 'call' : 'check' });

    const raise = el('button', 'btn small', 'Raise');
    raise.onclick = () => askRaise(g);
    if (g.balance <= g.toCall) raise.disabled = true;

    const fold = el('button', 'btn small', 'Fold');
    fold.onclick = () => pokerCall(g.id, 'action', { action: 'fold' });
    actions.append(stay, raise, fold);
  }
  if (g.stage === 'showdown' && g.youArePlaying) {
    const bots = g.seats.filter((s) => s.user && s.user.isBot).length;
    const again = el('button', 'btn small ghost', 'New hand');
    again.onclick = () => startPoker(g.ante, bots, g.messageId);
    actions.appendChild(again);
  }
  if (actions.children.length) card.appendChild(actions);

  const host = g.seats.find((s) => s.user && s.user.id === g.hostId);
  card.appendChild(el('div', 'game-foot',
    `${host && host.user ? host.user.username : 'someone'}'s table`));
  return card;
}

/** "How much on top?" — raises are whole antes, so offer them as buttons. */
function askRaise(g) {
  const steps = [];
  for (let n = g.minRaise; n <= g.maxRaise && steps.length < 6; n *= 2) steps.push(n);
  openModal((box, close) => {
    box.appendChild(el('h2', null, 'Raise'));
    box.appendChild(el('p', 'sub', g.toCall
      ? `${coins(g.toCall)} to call. Anything you add on top is the raise, and `
        + 'everyone still in has to answer it.'
      : 'Nothing to call. Whatever you put in becomes the price for everyone else.'));

    const form = el('form');
    const label = el('label', 'field');
    label.appendChild(el('span', null, 'Raise by'));
    const field = el('input');
    field.type = 'number';
    field.min = String(g.minRaise);
    field.max = String(g.maxRaise);
    field.step = String(g.minRaise);
    field.value = String(g.minRaise);
    label.appendChild(field);
    const total = el('small');
    const paint = () => {
      const n = parseInt(field.value, 10) || 0;
      total.textContent = `Costs you ${coins(g.toCall + n)} in total.`;
    };
    field.oninput = paint;
    paint();
    label.appendChild(total);
    form.appendChild(label);

    const quick = el('div', 'stake-quick');
    steps.forEach((n) => {
      const b = el('button', 'btn small ghost', n.toLocaleString());
      b.type = 'button';
      b.onclick = () => { field.value = String(n); paint(); };
      quick.appendChild(b);
    });
    form.appendChild(quick);

    const actions = el('div', 'modal-actions');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.type = 'button';
    cancel.onclick = close;
    const go = el('button', 'btn primary', 'Raise');
    actions.append(cancel, go);
    form.appendChild(actions);

    form.onsubmit = (e) => {
      e.preventDefault();
      const n = parseInt(field.value, 10);
      if (Number.isNaN(n) || n < g.minRaise) {
        toast(`The smallest raise is ${coins(g.minRaise)}.`, 'error');
        return;
      }
      close();
      pokerCall(g.id, 'action', { action: 'raise', raise: n });
    };
    box.appendChild(form);
  });
}

function purchaseCard(card, g) {
  const top = el('div', 'wallet-top');
  if (g.player) top.appendChild(avatar({ ...g.player, online: undefined }, 'lg'));
  const meta = el('div');
  meta.appendChild(nameNode(g.player, 'wallet-name'));
  meta.appendChild(el('div', 'muted', `now holding ${coins(g.coins)}`));
  top.appendChild(meta);
  card.appendChild(top);

  const banner = el('div', 'game-result win');
  banner.appendChild(el('strong', null, `${g.item.icon} ${g.item.name}`));
  banner.appendChild(el('span', null,
    `${g.item.summary} Paid ${coins(g.item.price)}.`));
  card.appendChild(banner);
  card.appendChild(el('div', 'game-foot', 'Bought from the Frontman shop — /shop'));
  return card;
}

function resetCard(card, g) {
  const banner = el('div', 'game-result lose');
  banner.appendChild(el('strong', null, 'Everything back to zero'));
  banner.appendChild(el('span', null,
    `${g.count} ${g.count === 1 ? 'account' : 'accounts'} in ${g.guild} reset to `
    + `${coins(g.startingCoins)}. Wins, losses and streaks wiped.`));
  card.appendChild(banner);
  card.appendChild(el('div', 'game-foot',
    `Called by ${g.player ? g.player.username : 'the owner'}. Perks bought from `
    + 'the shop were left alone.'));
  return card;
}

// Where each pocket sits on the layout, so a card can show the same grid the
// bet was placed on.
function rouletteNode(g) {
  const card = el('div', `game-card roulette ${g.profit > 0 ? 'won' : ''}`.trim());

  const head = el('div', 'game-head');
  head.appendChild(el('span', 'game-title', 'Roulette'));
  head.appendChild(el('span', 'game-stake', g.bet ? coins(g.bet) : 'for fun'));
  head.appendChild(balanceChip(g));
  head.appendChild(el('span', 'game-status', `${g.betLabel} · pays ${g.pays} to 1`));
  card.appendChild(head);

  const pocket = el('div', `pocket ${g.colour}`);
  pocket.appendChild(el('span', 'pocket-number', String(g.number)));
  pocket.appendChild(el('span', 'pocket-colour', g.colour));
  const wheel = el('div', 'wheel');
  wheel.appendChild(pocket);
  card.appendChild(wheel);

  const banner = el('div', `game-result ${g.profit > 0 ? 'win' : 'lose'}`);
  banner.appendChild(el('strong', null, g.profit > 0
    ? `+${g.profit.toLocaleString()} ${COIN}`
    : `-${g.bet.toLocaleString()} ${COIN}`));
  banner.appendChild(el('span', null, g.label));
  card.appendChild(banner);

  if (g.yourSeat) {
    const actions = el('div', 'game-actions');
    const again = el('button', 'btn small ghost', 'Spin again');
    again.onclick = () => askRoulette(g.bet || 100, g.messageId);
    actions.appendChild(again);
    card.appendChild(actions);
  }

  card.appendChild(el('div', 'game-foot',
    `${g.player ? g.player.username : 'someone'}'s spin`));
  return card;
}

function begNode(g) {
  const card = el('div', `game-card beg ${g.raised ? 'won' : ''}`.trim());

  const head = el('div', 'game-head');
  head.appendChild(el('span', 'game-title', 'Spare any change?'));
  head.appendChild(balanceChip(g));
  head.appendChild(el('span', 'game-status',
    g.raised ? `raised ${g.raised.toLocaleString()} ${COIN}` : 'nothing yet'));
  card.appendChild(head);

  const top = el('div', 'wallet-top');
  if (g.player) top.appendChild(avatar({ ...g.player, online: undefined }, 'lg'));
  const meta = el('div');
  meta.appendChild(nameNode(g.player, 'wallet-name'));
  meta.appendChild(el('div', 'muted', 'is asking for donations.'));
  top.appendChild(meta);
  card.appendChild(top);

  const banner = el('div', `game-result ${g.botGave ? 'win' : 'lose'}`);
  banner.appendChild(el('strong', null, g.botGave
    ? `+${g.botGave.toLocaleString()} ${COIN} from the Frontman`
    : 'The Frontman gives nothing'));
  banner.appendChild(el('span', null, `It ${g.line}.`));
  card.appendChild(banner);

  if (g.donations.length) {
    const list = el('div', 'beg-list');
    g.donations.forEach((d) => {
      const row = el('div', 'beg-row');
      if (d.user) row.appendChild(avatar({ ...d.user, online: undefined }, 'xs'));
      row.appendChild(el('span', 'beg-who', d.user ? d.user.username : 'Someone'));
      row.appendChild(el('span', 'beg-amount', `${d.amount.toLocaleString()} ${COIN}`));
      list.appendChild(row);
    });
    card.appendChild(list);
  }

  if (g.canGive) {
    const actions = el('div', 'game-actions');
    const give = el('button', 'btn primary small', 'Give something');
    give.onclick = () => askDonation(g);
    actions.appendChild(give);
    card.appendChild(actions);
  }

  card.appendChild(el('div', 'game-foot',
    `${g.player ? g.player.username : 'Someone'} passed the hat round`));
  return card;
}

/** How much to drop in the hat. Entirely up to whoever is giving. */
function askDonation(g) {
  const have = state.wallet ? state.wallet.coins : g.balance || 0;
  openModal((box, close) => {
    box.appendChild(el('h2', null, 'Give something'));
    box.appendChild(el('p', 'sub',
      `Straight out of your own pocket to ${g.player ? g.player.username : 'them'}. `
      + `You have ${coins(have)}.`));

    const form = el('form');
    const label = el('label', 'field');
    label.appendChild(el('span', null, 'Amount'));
    const field = el('input');
    field.type = 'number';
    field.min = '1';
    field.value = String(Math.min(500, have));
    label.appendChild(field);
    form.appendChild(label);

    const quick = el('div', 'stake-quick');
    [100, 500, 1000, 5000].filter((n) => n <= have).forEach((n) => {
      const b = el('button', 'btn small ghost', n.toLocaleString());
      b.type = 'button';
      b.onclick = () => { field.value = String(n); };
      quick.appendChild(b);
    });
    form.appendChild(quick);

    const actions = el('div', 'modal-actions');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.type = 'button';
    cancel.onclick = close;
    const go = el('button', 'btn primary', 'Give');
    actions.append(cancel, go);
    form.appendChild(actions);

    form.onsubmit = async (e) => {
      e.preventDefault();
      const n = parseInt(field.value, 10);
      if (!n || n < 1) return toast('Enter an amount.', 'error');
      go.disabled = true;
      try {
        const data = await api('POST', `/api/games/${g.id}/beg/give`, { amount: n });
        if (data.wallet) state.wallet = data.wallet;
        replaceMessage(data.message);
        close();
      } catch (err) { toast(err.message, 'error'); go.disabled = false; }
    };
    box.appendChild(form);
  });
}

function statLine(label, value) {
  const row = el('div', 'stat');
  row.appendChild(el('span', 'stat-label', label));
  row.appendChild(el('span', 'stat-value', value));
  return row;
}

function infoCardNode(g) {
  const card = el('div', 'game-card info');
  const head = el('div', 'game-head');
  const titles = {
    claim: 'Daily claim', balance: 'Wallet', work: 'Shift finished',
    bank: 'Sana Coin bank', leaderboard: 'Leaderboard',
    purchase: 'Sana Coin shop', reset: 'Economy reset',
  };
  head.appendChild(el('span', 'game-title', titles[g.kind] || 'Frontman'));
  card.appendChild(head);

  if (g.kind === 'purchase') return purchaseCard(card, g);
  if (g.kind === 'reset') return resetCard(card, g);

  if (g.kind === 'claim' || g.kind === 'balance' || g.kind === 'work') {
    const top = el('div', 'wallet-top');
    if (g.player) top.appendChild(avatar({ ...g.player, online: undefined }, 'lg'));
    const meta = el('div');
    meta.appendChild(nameNode(g.player, 'wallet-name'));
    meta.appendChild(el('div', 'muted', `${g.stats.rank} · ${coins(g.coins)}`));
    top.appendChild(meta);
    card.appendChild(top);

    if (g.kind === 'claim') {
      const b = el('div', 'game-result win');
      b.appendChild(el('strong', null, `+${g.claimed.toLocaleString()} ${COIN}`));
      b.appendChild(el('span', null, 'Daily claim collected. Come back tomorrow.'));
      card.appendChild(b);
    }

    if (g.kind === 'work') {
      const b = el('div', `game-result ${g.ok ? 'win' : 'lose'}`);
      b.appendChild(el('strong', null, g.ok
        ? `+${g.earned.toLocaleString()} ${COIN}`
        : 'No pay this shift'));
      b.appendChild(el('span', null, `You ${g.shift}.`));
      card.appendChild(b);
      if (g.raised) {
        const rise = el('div', 'game-result win');
        rise.appendChild(el('strong', null, '↑ Pay rise'));
        rise.appendChild(el('span', null,
          `Five more hours on the clock. You're on ${coins(g.workPay)} a shift now.`));
        card.appendChild(rise);
      }
    }

    const grid = el('div', 'stat-grid');
    grid.appendChild(statLine('Won', String(g.stats.wins)));
    grid.appendChild(statLine('Lost', String(g.stats.losses)));
    grid.appendChild(statLine('Pushed', String(g.stats.pushes)));
    grid.appendChild(statLine('Net',
      `${g.stats.net >= 0 ? '+' : ''}${g.stats.net.toLocaleString()}`));
    grid.appendChild(statLine('Best win', g.stats.biggestWin.toLocaleString()));
    grid.appendChild(statLine('Best streak', String(g.stats.bestStreak)));
    card.appendChild(grid);

    if (g.kind === 'balance') {
      card.appendChild(el('div', 'game-foot', g.canClaim
        ? 'Daily claim is ready — use /claim'
        : `Next claim in ${formatWait(g.claimIn)}`));
    }
    if (g.kind === 'work') {
      card.appendChild(el('div', 'game-foot',
        `Back on the clock in ${formatWait(g.workIn)}.`));
    }
    return card;
  }

  const rows = g.kind === 'bank' ? g.accounts : g.players;
  if (g.kind === 'bank') {
    card.appendChild(el('p', 'muted',
      `${coins(g.total)} across ${rows.length} account${rows.length === 1 ? '' : 's'}.`));
  }
  const list = el('div', 'rank-list');
  (rows || []).forEach((r, i) => {
    const row = el('div', 'rank-row');
    const pos = el('span', 'rank-pos', String(i + 1));
    if (i < 3) pos.classList.add('podium');
    row.appendChild(pos);
    row.appendChild(avatar({ ...r, online: undefined }));
    const who = el('div', 'rank-who');
    who.appendChild(nameNode(r, 'rank-name'));
    who.appendChild(el('small', null, g.kind === 'bank'
      ? r.rank
      : `${r.rank} · ${r.wins}W / ${r.losses}L · best streak ${r.bestStreak}`));
    row.appendChild(who);
    if (g.kind === 'bank') {
      row.appendChild(el('span', 'rank-score', coins(r.coins)));
    } else {
      row.appendChild(el('span', `rank-score ${r.net >= 0 ? 'up' : 'down'}`,
        `${r.net >= 0 ? '+' : ''}${r.net.toLocaleString()}`));
    }
    list.appendChild(row);
  });
  if (!rows || !rows.length) list.appendChild(el('p', 'muted', 'Nobody has played yet.'));
  card.appendChild(list);
  return card;
}

function gameNode(m) {
  const g = m.game;
  if (g.mode === 'info') return infoCardNode(g);
  if (g.mode === 'poker') return pokerNode(g);
  if (g.mode === 'slots') return slotsNode(g);
  if (g.mode === 'roulette') return rouletteNode(g);
  if (g.mode === 'beg') return begNode(g);

  const card = el('div', `game-card ${g.status}`.trim());

  const head = el('div', 'game-head');
  head.appendChild(el('span', 'game-title',
    g.mode === 'cpu' ? 'Blackjack' : 'Blackjack · 1v1'));
  if (g.bet) {
    const stake = el('span', 'game-stake', coins(g.bet));
    stake.title = g.mode === 'pvp' ? `Pot: ${coins(g.bet * 2)}` : 'Your stake';
    head.appendChild(stake);
  }
  head.appendChild(balanceChip(g));
  const status = el('span', 'game-status');
  if (g.status === 'waiting') status.textContent = 'Waiting for an opponent';
  else if (g.status === 'finished') status.textContent = 'Finished';
  else if (g.turn) {
    const who = g.seats[g.turn].user;
    status.textContent = g.yourTurn ? 'Your turn'
      : `${who ? who.username : 'Dealer'} to play`;
  }
  head.appendChild(status);
  card.appendChild(head);

  card.appendChild(seatRow(g.seats.host, 'Host', g.status === 'playing' && g.turn === 'host'));
  const oppLabel = g.mode === 'cpu' ? 'Dealer' : 'Waiting…';
  card.appendChild(seatRow(g.seats.opp, oppLabel, g.status === 'playing' && g.turn === 'opp'));

  if (g.result) {
    const banner = el('div', `game-result ${g.outcome || ''}`.trim());
    const headline = { win: 'You won!', lose: 'You lost', push: 'Push' }[g.outcome]
      || (g.result.winner === 'host' ? `${g.seats.host.user.username} won`
                                     : `${g.seats.opp.user ? g.seats.opp.user.username : 'Dealer'} won`);
    banner.appendChild(el('strong', null, headline));
    banner.appendChild(el('span', null, g.result.text));
    card.appendChild(banner);
  }

  const actions = el('div', 'game-actions');
  if (g.canJoin) {
    const join = el('button', 'btn primary small',
      g.bet ? `Join for ${coins(g.bet)}` : 'Join game');
    join.onclick = async () => {
      join.disabled = true;
      try {
        const data = await api('POST', `/api/games/${g.id}/join`);
        if (data.wallet) state.wallet = data.wallet;
        replaceMessage(data.message);
      } catch (err) { toast(err.message, 'error'); join.disabled = false; }
    };
    actions.appendChild(join);
  }
  if (g.yourTurn) {
    const moves = [['hit', 'Hit'], ['stand', 'Stand']];
    // Doubling and splitting each put a second stake up, so they say so.
    if (g.canDouble) moves.push(['double', g.bet ? `Double (${coins(g.bet)})` : 'Double']);
    if (g.canSplit) moves.push(['split', g.bet ? `Split (${coins(g.bet)})` : 'Split']);
    moves.forEach(([action, label]) => {
      const b = el('button', `btn small ${action === 'hit' ? 'primary' : ''}`.trim(), label);
      b.onclick = async () => {
        [...actions.querySelectorAll('button')].forEach((x) => { x.disabled = true; });
        try {
          const data = await api('POST', `/api/games/${g.id}/action`, { action });
          if (data.wallet) state.wallet = data.wallet;
          replaceMessage(data.message);
        } catch (err) {
          toast(err.message, 'error');
          [...actions.querySelectorAll('button')].forEach((x) => { x.disabled = false; });
        }
      };
      actions.appendChild(b);
    });
  }
  if (g.status === 'finished' && g.yourSeat !== null) {
    const again = el('button', 'btn small ghost',
      g.bet ? `Play again for ${coins(g.bet)}` : 'Play again');
    again.onclick = () => startGame(g.mode, g.bet, g.messageId);
    actions.appendChild(again);
  }
  if (actions.children.length) card.appendChild(actions);

  const foot = el('div', 'game-foot');
  const hostName = g.seats.host.user ? g.seats.host.user.username : 'someone';
  foot.textContent = `${hostName}'s game`;
  card.appendChild(foot);
  return card;
}

// --------------------------------------------------------------- approvals

const SIGNUP_WORD = { approved: 'Approved', declined: 'Declined', pending: 'Waiting' };

function signupNode(m) {
  const a = m.signup;
  const card = el('div', `signup-card ${a.status}`);

  const head = el('div', 'game-head');
  head.appendChild(el('span', 'game-title', 'New sign-up'));
  head.appendChild(el('span', 'game-status', SIGNUP_WORD[a.status] || a.status));
  card.appendChild(head);

  if (!a.user) {
    card.appendChild(el('p', 'muted', 'That account no longer exists.'));
    return card;
  }

  const top = el('div', 'wallet-top');
  top.appendChild(avatar({ ...a.user, online: undefined }, 'lg'));
  const meta = el('div');
  // The real name leads: it's what the admin is actually checking.
  meta.appendChild(el('strong', null, a.user.fullName || a.user.username));
  meta.appendChild(el('div', 'muted', `${a.user.tag} · ${a.user.email}`));
  meta.appendChild(el('small', 'muted', `Applied ${stampLabel(a.createdAt)}`));
  top.appendChild(meta);
  card.appendChild(top);

  if (a.canDecide) {
    const actions = el('div', 'game-actions');
    const yes = el('button', 'btn primary small', 'Approve');
    const no = el('button', 'btn small danger', 'Decline');
    const decide = async (verdict, button) => {
      [yes, no].forEach((b) => { b.disabled = true; });
      try {
        const data = await api('POST', `/api/signups/${a.id}/decide`, { verdict });
        if (data.message) replaceMessage(data.message);
        toast(verdict === 'approve'
          ? `${a.user.username} is in.`
          : `${a.user.username} was turned away.`, 'ok');
      } catch (err) {
        toast(err.message, 'error');
        [yes, no].forEach((b) => { b.disabled = false; });
      }
    };
    yes.onclick = () => decide('approve', yes);
    no.onclick = () => decide('decline', no);
    actions.append(yes, no);
    card.appendChild(actions);
  } else if (a.status !== 'pending') {
    const banner = el('div', `game-result ${a.status === 'approved' ? 'win' : 'lose'}`);
    banner.appendChild(el('strong', null,
      a.status === 'approved' ? 'Let in' : 'Turned away'));
    banner.appendChild(el('span', null,
      `${a.decidedBy || 'An administrator'} decided${
        a.decidedAt ? ` ${stampLabel(a.decidedAt)}` : ''}.`));
    card.appendChild(banner);
  }
  return card;
}

// ------------------------------------------------------------------- polls

function pollNode(m) {
  const p = m.poll;
  const card = el('div', `poll-card ${p.closed ? 'closed' : ''}`.trim());

  const head = el('div', 'game-head');
  head.appendChild(el('span', 'game-title', p.closed ? 'Poll · closed' : 'Poll'));
  head.appendChild(el('span', 'game-status',
    `${p.voters} ${p.voters === 1 ? 'vote' : 'votes'}`
    + (p.multi ? ' · pick as many as you like' : '')));
  card.appendChild(head);
  card.appendChild(el('h4', 'poll-question', p.question));

  // The leader is only meaningful once somebody has voted, and only when it
  // isn't a tie — otherwise every option looks like it's winning.
  const top = Math.max(...p.options.map((o) => o.votes));
  const leaders = p.options.filter((o) => o.votes === top && top > 0);

  p.options.forEach((o) => {
    const row = el('button', `poll-option ${o.mine ? 'mine' : ''}`.trim());
    if (leaders.length === 1 && leaders[0] === o) row.classList.add('leading');
    row.disabled = p.closed;

    const fill = el('span', 'poll-fill');
    fill.style.width = `${o.share}%`;
    row.appendChild(fill);

    const label = el('span', 'poll-label');
    label.appendChild(el('span', 'poll-tick', o.mine ? '✓' : ''));
    label.appendChild(el('span', 'poll-text', o.label));
    row.appendChild(label);

    row.appendChild(el('span', 'poll-count', `${o.votes} · ${o.share}%`));
    if (!p.closed) row.onclick = () => votePoll(p.id, o.index);
    card.appendChild(row);
  });

  const foot = el('div', 'poll-foot');
  foot.appendChild(el('span', 'muted',
    p.closed ? 'Final result.'
             : (p.multi ? 'Click again to take a vote back.'
                        : 'One vote each — click again to take it back.')));
  if (p.isAuthor && !p.closed) {
    const close = el('button', 'linkbtn', 'Close poll');
    close.onclick = () => confirmModal('Close this poll?',
      'Nobody will be able to vote after this, and the result stays on show.',
      'Close poll', async () => {
        const data = await api('POST', `/api/polls/${p.id}/close`);
        replaceMessage(data.message);
      }, false);
    foot.appendChild(close);
  }
  card.appendChild(foot);
  return card;
}

async function votePoll(pollId, choice) {
  try {
    const data = await api('POST', `/api/polls/${pollId}/vote`, { choice });
    replaceMessage(data.message);
  } catch (err) { toast(err.message, 'error'); }
}

/** Swap one message in place and redraw, keeping the scroll position. */
function replaceMessage(message) {
  const idx = state.messages.findIndex((x) => x.id === message.id);
  if (idx >= 0) state.messages[idx] = message;
  else state.messages.push(message);
  renderMessages();
}

// ------------------------------------------------------- attachments & stickers

function attachmentNode(a) {
  const wrap = el('div', 'attachment');
  const img = el('img');
  img.src = a.url;
  img.alt = a.name || 'image';
  img.loading = 'lazy';
  // Reserve the right box before the bytes arrive so the list doesn't jump.
  if (a.width && a.height) {
    const scale = Math.min(1, 400 / a.height, 520 / a.width);
    img.width = Math.round(a.width * scale);
    img.height = Math.round(a.height * scale);
  }
  img.onclick = () => openLightbox(a);
  wrap.appendChild(img);
  return wrap;
}

function stickerNode(sticker) {
  const wrap = el('div', 'sticker');
  const img = el('img');
  img.src = sticker.url;
  img.alt = sticker.name;
  img.title = `:${sticker.name}:`;
  wrap.appendChild(img);
  return wrap;
}

function openLightbox(a) {
  const back = el('div', 'lightbox');
  const img = el('img');
  img.src = a.url;
  img.alt = a.name || '';
  back.appendChild(img);

  const bar = el('div', 'lightbox-bar');
  const open = el('a', 'linkbtn', 'Open original');
  open.href = a.url;
  open.target = '_blank';
  open.rel = 'noopener noreferrer';
  bar.append(el('span', 'muted', `${a.name || 'image'} · ${formatBytes(a.size)}`), open);
  back.appendChild(bar);

  const close = () => {
    back.remove();
    document.removeEventListener('keydown', onKey);
  };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  back.onclick = (e) => { if (e.target !== img) close(); };
  document.addEventListener('keydown', onKey);
  document.body.appendChild(back);
}

function formatBytes(n) {
  if (!n) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// ------------------------------------------------------------------ reactions

async function react_(messageId, emoji) {
  try {
    const data = await api('POST', `/api/messages/${messageId}/reactions`, { emoji });
    const idx = state.messages.findIndex((x) => x.id === messageId);
    if (idx >= 0) {
      state.messages[idx] = data.message;
      renderMessages();
    }
  } catch (err) { toast(err.message, 'error'); }
}

function reactionBar(m) {
  const bar = el('div', 'reactions');
  m.reactions.forEach((r) => {
    const pill = el('button', `pill ${r.me ? 'mine' : ''}`.trim());
    pill.appendChild(el('span', 'pill-emoji', r.emoji));
    pill.appendChild(el('span', 'pill-count', String(r.count)));
    pill.onclick = () => react_(m.id, r.emoji);
    pill.onmouseenter = () => showReactors(pill, m.id, r.emoji);
    pill.onmouseleave = hideReactors;
    // Keyboard and touch reach it too: focus opens the same card.
    pill.onfocus = () => showReactors(pill, m.id, r.emoji);
    pill.onblur = hideReactors;
    bar.appendChild(pill);
  });

  const add = el('button', 'pill add-pill', '＋');
  add.title = 'Add reaction';
  add.onclick = (e) => openEmojiPicker(e.currentTarget, (emoji) => react_(m.id, emoji));
  bar.appendChild(add);
  return bar;
}

// ---------------------------------------------------- who reacted with what

let reactorCard = null;
let reactorToken = 0;

function hideReactors() {
  reactorToken += 1;                    // any in-flight fetch is now stale
  if (reactorCard) { reactorCard.remove(); reactorCard = null; }
}

/**
 * Hover card listing everyone behind a reaction pill.
 *
 * Refetched on every hover rather than cached: people add and remove
 * reactions while you are looking at the message, and a list that was right
 * once is worse than no list at all.
 */
async function showReactors(anchor, messageId, emoji) {
  hideReactors();
  const mine = ++reactorToken;

  const card = el('div', 'popover reactor-pop');
  card.appendChild(el('div', 'pop-title', `Reacted with ${emoji}`));
  card.appendChild(el('div', 'muted', 'Loading…'));
  document.body.appendChild(card);
  reactorCard = card;
  placeReactors(card, anchor);

  let people = [];
  try {
    const data = await api('GET', `/api/messages/${messageId}/reactions`);
    people = data.reactors[emoji] || [];
  } catch {
    if (mine === reactorToken && reactorCard === card) {
      card.replaceChildren(el('div', 'muted', 'Could not load that just now.'));
    }
    return;
  }
  // The pointer may have moved on, or moved to a different pill, while we
  // were waiting: only the newest request may draw.
  if (mine !== reactorToken || reactorCard !== card) return;

  card.replaceChildren();
  card.appendChild(el('div', 'pop-title', `${people.length} reacted with ${emoji}`));
  const list = el('div', 'reactor-list');
  people.slice(0, 12).forEach((u) => {
    const row = el('div', 'reactor');
    row.appendChild(avatar({ ...u, online: undefined }, 'xs'));
    row.appendChild(el('span', null, u.username));
    list.appendChild(row);
  });
  if (people.length > 12) {
    list.appendChild(el('div', 'muted', `and ${people.length - 12} more`));
  }
  card.appendChild(list);
  placeReactors(card, anchor);
}

function placeReactors(card, anchor) {
  const box = anchor.getBoundingClientRect();
  const rect = card.getBoundingClientRect();
  card.style.left = `${Math.max(8,
    Math.min(box.left, window.innerWidth - rect.width - 8))}px`;
  // Above the pill by default; below it when there is no room up there.
  const above = box.top - rect.height - 8;
  card.style.top = `${above < 8 ? box.bottom + 8 : above}px`;
}

// A small curated set — enough to be useful without shipping an emoji library.
const EMOJI = [
  '👍', '👎', '❤️', '🔥', '🎉', '😂', '😮', '😢', '😡', '🙏',
  '👀', '✅', '❌', '💯', '🚀', '⭐', '💡', '🤔', '😎', '🥳',
  '👋', '🙌', '💀', '🤝', '☕', '🍕', '🎵', '⚡', '🌈', '🐛',
];

let openPopover = null;

function closePopover() {
  if (openPopover) { openPopover.remove(); openPopover = null; }
}

document.addEventListener('click', (e) => {
  if (openPopover && !openPopover.contains(e.target)
      && !e.target.closest('[data-popover-anchor]')) {
    closePopover();
  }
});

function placePopover(pop, anchor) {
  document.body.appendChild(pop);
  const box = anchor.getBoundingClientRect();
  const rect = pop.getBoundingClientRect();
  let left = Math.min(box.left, window.innerWidth - rect.width - 12);
  let top = box.top - rect.height - 8;
  if (top < 12) top = Math.min(box.bottom + 8, window.innerHeight - rect.height - 12);
  pop.style.left = `${Math.max(12, left)}px`;
  pop.style.top = `${Math.max(12, top)}px`;
}

function openEmojiPicker(anchor, onPick) {
  closePopover();
  anchor.setAttribute('data-popover-anchor', '');
  const pop = el('div', 'popover emoji-pop');
  EMOJI.forEach((e) => {
    const b = el('button', 'emoji-btn', e);
    b.onclick = () => { onPick(e); closePopover(); };
    pop.appendChild(b);
  });
  openPopover = pop;
  placePopover(pop, anchor);
}

function startEdit(m, body, textNode) {
  const box = el('textarea');
  box.value = m.content;
  box.style.cssText =
    'width:100%;background:var(--bg-3);border:0;outline:none;resize:none;' +
    'border-radius:8px;padding:10px 12px;line-height:1.45;font:inherit;color:inherit';
  const hint = el('div', 'muted');
  hint.style.fontSize = '12px';
  hint.style.marginTop = '4px';
  hint.textContent = 'Enter to save · Escape to cancel';
  textNode.replaceWith(box);
  body.appendChild(hint);
  box.focus();
  box.setSelectionRange(box.value.length, box.value.length);
  box.style.height = `${box.scrollHeight}px`;

  const cancel = () => { box.replaceWith(textNode); hint.remove(); };
  box.onkeydown = async (e) => {
    if (e.key === 'Escape') { e.preventDefault(); cancel(); }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      const content = box.value.trim();
      if (!content) return cancel();
      try {
        const data = await api('PATCH', `/api/messages/${m.id}`, { content });
        const idx = state.messages.findIndex((x) => x.id === m.id);
        if (idx >= 0) state.messages[idx] = data.message;
        renderMessages();
      } catch (err) { toast(err.message, 'error'); }
    }
  };
}

function emptyChannelNotice() {
  const wrap = el('div', 'empty');
  const ch = state.activeChannel;
  if (!ch) return wrap;
  if (ch.kind === 'dm') {
    wrap.appendChild(avatar({ ...ch.user, online: undefined }, 'xl'));
    wrap.appendChild(el('h2', null, ch.user ? ch.user.username : 'Direct message'));
    wrap.appendChild(el('p', null,
      `This is the start of your conversation with ${ch.user ? ch.user.tag : 'them'}.`));
  } else {
    wrap.appendChild(el('div', 'big-icon', '#'));
    wrap.appendChild(el('h2', null, `Welcome to #${ch.name}`));
    wrap.appendChild(el('p', null, 'This is the beginning of the channel. Say something.'));
  }
  return wrap;
}

function scrollToBottom() {
  const box = $('#messages');
  box.scrollTop = box.scrollHeight;
  state.atBottom = true;
}

$('#messages').addEventListener('scroll', () => {
  const box = $('#messages');
  state.atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
});

// ------------------------------------------------------------------ friends view

function renderFriendsView() {
  const box = $('#messages');
  box.replaceChildren();
  $('#composer').classList.add('hidden-composer');
  $('#topbar-title').firstElementChild.textContent = '☺';
  $('#channel-name').textContent = 'Friends';
  $('#channel-topic').textContent = '';
  $('#invite-btn').hidden = true;
  $('#members-panel').hidden = true;
  $('#members-btn').hidden = true;

  const view = el('div', 'friends-view');

  const addBox = el('div', 'add-friend-box');
  addBox.appendChild(el('h2', null, 'Add a friend'));
  addBox.appendChild(el('p', null,
    `Friends are found by their full tag, including the four digits. Yours is ${state.me.tag}.`));
  const form = el('form', 'inline-form');
  const input = el('input');
  input.placeholder = 'Alex#0421';
  input.maxLength = 64;
  const submit = el('button', 'btn primary', 'Send request');
  submit.type = 'submit';
  form.append(input, submit);
  form.onsubmit = async (e) => {
    e.preventDefault();
    if (!input.value.trim()) return;
    submit.disabled = true;
    try {
      const res = await api('POST', '/api/friends', { tag: input.value.trim() });
      input.value = '';
      toast(res.status === 'accepted'
        ? 'You are now friends!'
        : 'Friend request sent.', 'ok');
      await refreshAll();
    } catch (err) { toast(err.message, 'error'); }
    finally { submit.disabled = false; }
  };
  addBox.appendChild(form);
  view.appendChild(addBox);

  const { friends, incoming, outgoing } = state.friends;

  if (incoming.length) {
    view.appendChild(el('h3', null, `Pending — ${incoming.length}`));
    incoming.forEach((f) => view.appendChild(friendRow(f, 'incoming')));
  }
  if (outgoing.length) {
    view.appendChild(el('h3', null, `Sent — ${outgoing.length}`));
    outgoing.forEach((f) => view.appendChild(friendRow(f, 'outgoing')));
  }

  view.appendChild(el('h3', null, `All friends — ${friends.length}`));
  if (!friends.length) {
    view.appendChild(el('p', 'muted', 'No friends yet. Send a request using the box above.'));
  }
  friends.forEach((f) => view.appendChild(friendRow(f, 'friend')));

  box.appendChild(view);
}

function friendRow(f, kind) {
  const row = el('div', 'friend-row');
  row.appendChild(avatar(f, 'lg'));
  const name = el('div', 'f-name');
  name.appendChild(el('strong', null, f.username));
  name.appendChild(el('small', null,
    kind === 'friend' ? `${f.tag} · ${f.online ? 'Online' : 'Offline'}` : f.tag));
  row.appendChild(name);

  const actions = el('div', 'friend-actions');
  const mk = (cls, label, title, fn) => {
    const b = el('button', `round-btn ${cls}`, label);
    b.title = title;
    b.onclick = fn;
    return b;
  };

  if (kind === 'friend') {
    actions.appendChild(mk('', '✉', 'Message', async () => {
      try {
        const d = await api('POST', '/api/dms', { userId: f.id });
        await refreshAll();
        openChannel(d.channelId);
      } catch (err) { toast(err.message, 'error'); }
    }));
    actions.appendChild(mk('no', '✕', 'Remove friend', () =>
      confirmModal('Remove friend?', `${f.tag} will be removed from your friends list.`,
        'Remove', async () => {
          await api('DELETE', `/api/friends/${f.friendshipId}`);
          await refreshAll();
        })));
  } else if (kind === 'incoming') {
    actions.appendChild(mk('ok', '✓', 'Accept', async () => {
      await api('POST', `/api/friends/${f.friendshipId}/accept`);
      toast(`You and ${f.username} are now friends.`, 'ok');
      await refreshAll();
    }));
    actions.appendChild(mk('no', '✕', 'Ignore', async () => {
      await api('DELETE', `/api/friends/${f.friendshipId}`);
      await refreshAll();
    }));
  } else {
    actions.appendChild(mk('no', '✕', 'Cancel request', async () => {
      await api('DELETE', `/api/friends/${f.friendshipId}`);
      await refreshAll();
    }));
  }
  row.appendChild(actions);
  return row;
}

// ------------------------------------------------------------------ navigation

/** Say what a channel's rules are, in the topic line and the composer, so
    nobody discovers them by being refused. */
function paintChannelRules() {
  const ch = state.activeChannel;
  if (!ch || ch.kind !== 'text') return;
  const notes = [];
  if (ch.slowMode) {
    const step = SLOW_STEPS.find(([s]) => s === ch.slowMode);
    notes.push(`Slow mode: ${step ? step[1].toLowerCase() : `${ch.slowMode}s`}`);
  }
  if (ch.pollsOnly) notes.push('Polls only');
  if (ch.noBots) notes.push('No bots');

  const topic = $('#channel-topic');
  topic.textContent = [ch.topic, notes.join(' · ')].filter(Boolean).join(' — ');

  const input = $('#composer-input');
  if (ch.pollsOnly) input.placeholder = `Only polls can be posted in #${ch.name}`;
  else if (ch.slowMode) input.placeholder = `Message #${ch.name} — slow mode is on`;
  else input.placeholder = `Message #${ch.name}`;
}

function setMobileView(inChannel) {
  document.getElementById('app').classList.toggle('viewing-channel', inChannel);
}

async function openGuild(guildId) {
  state.activeGuildId = guildId;
  const guild = state.guilds.find((g) => g.id === guildId);
  const first = guild && guild.channels[0];
  renderRail();
  renderSidebar();
  if (first) await openChannel(first.id);
}

function openFriends() {
  state.activeGuildId = null;
  state.activeChannelId = null;
  state.activeChannel = null;
  state.messages = [];
  stopPoll();
  renderRail();
  renderSidebar();
  renderFriendsView();
  setMobileView(false);
  startPoll();
}

async function openChannel(channelId) {
  state.activeChannelId = channelId;
  state.channelRev = '';
  closePopover();
  closeMentions();
  cancelReply();
  stopPoll();
  loadMentionable(channelId);
  $('#composer').classList.remove('hidden-composer');
  // Clear any inline height and let CSS supply the resting size. Measuring
  // here would be wrong anyway: during boot the chat pane has no height yet
  // and scrollHeight comes back nonsense.
  input.style.height = '';
  setMobileView(true);

  try {
    const info = await api('GET', `/api/channels/${channelId}`);
    state.activeChannel = info.channel;
  } catch (err) {
    toast(err.message, 'error');
    return openFriends();
  }

  const ch = state.activeChannel;
  if (ch.kind === 'dm') {
    state.activeGuildId = null;
    $('#topbar-title').firstElementChild.textContent = '@';
    $('#channel-name').textContent = ch.user ? ch.user.username : 'Direct message';
    $('#channel-topic').textContent = ch.user ? ch.user.tag : '';
    $('#invite-btn').hidden = true;
    $('#composer-input').placeholder = `Message @${ch.user ? ch.user.username : ''}`;
  } else {
    state.activeGuildId = ch.guildId;
    $('#topbar-title').firstElementChild.textContent = '#';
    $('#channel-name').textContent = ch.name;
    $('#channel-topic').textContent = ch.topic || '';
    $('#invite-btn').hidden = false;
    $('#composer-input').placeholder = `Message #${ch.name}`;
    paintChannelRules();
  }

  try {
    const data = await api('GET', `/api/channels/${channelId}/messages`);
    state.messages = data.messages;
  } catch (err) {
    toast(err.message, 'error');
    state.messages = [];
  }

  state.atBottom = true;
  renderRail();
  renderSidebar();
  renderMessages();
  scrollToBottom();
  renderMembers();
  refreshSidebarData().then(() => { renderRail(); renderSidebar(); });
  startPoll();
  $('#composer-input').focus();
}

$('#rail-home').onclick = () => {
  if (state.dms.length) openChannel(state.dms[0].channelId);
  else openFriends();
};
$('#back-btn').onclick = () => setMobileView(false);
$('#members-btn').onclick = () =>
  $('#members-panel').classList.toggle('force-open');

// ------------------------------------------------------------------ composer

const input = $('#composer-input');

function autosize() {
  // While the composer is hidden (friends view) every measurement reads 0,
  // which would pin the box shut once it comes back. Leave it alone instead.
  if (!input.offsetParent) return;
  input.style.height = 'auto';
  input.style.height = `${Math.min(Math.max(input.scrollHeight, 24), 180)}px`;
}
input.addEventListener('input', autosize);

input.addEventListener('keydown', (e) => {
  // The command list takes the keys first when it's showing.
  if (commandBox && commandMatches.length) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const step = e.key === 'ArrowDown' ? 1 : -1;
      commandIndex = (commandIndex + step + commandMatches.length) % commandMatches.length;
      paintCommandSelection();
      return;
    }
    if (e.key === 'Tab') {
      e.preventDefault();
      const c = commandMatches[commandIndex];
      input.value = `${c.name} ${c.args}`;
      autosize();
      renderCommandBox();
      return;
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      // If they've already typed a full command with its argument, honour
      // that — otherwise run whatever is highlighted in the list.
      const typed = matchCommand(input.value);
      if (typed && !typed.unknown && typed.arg != null) runCommand(typed.cmd, typed.arg);
      else runCommand(commandMatches[commandIndex]);
      return;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      closeCommands();
      return;
    }
  }
  // While the @ list is open it owns the arrows, Tab, Enter and Escape.
  if (mentionBox && mentionMatches.length) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const step = e.key === 'ArrowDown' ? 1 : -1;
      mentionIndex = (mentionIndex + step + mentionMatches.length) % mentionMatches.length;
      paintMentionSelection();
      return;
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault();
      applyMention(mentionMatches[mentionIndex]);
      return;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      closeMentions();
      return;
    }
  }
  if (e.key === 'Escape' && state.replyTo) {
    e.preventDefault();
    cancelReply();
    return;
  }
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});
$('#send-btn').onclick = sendMessage;

async function sendMessage() {
  const content = input.value.trim();
  if (!content || !state.activeChannelId) return;

  // A leading slash runs a command rather than posting text.
  const hit = matchCommand(content);
  if (hit) {
    if (hit.unknown) {
      toast(`${hit.unknown} isn't a command. Try /blackjack.`, 'error');
      return;
    }
    if (!commandAvailable(hit.cmd)) {
      toast(`Only the server owner can use ${hit.cmd.name}.`, 'error');
      return;
    }
    runCommand(hit.cmd, hit.arg);
    return;
  }

  const replyTo = state.replyTo ? state.replyTo.id : null;
  input.value = '';
  autosize();
  cancelReply();
  try {
    const data = await api('POST', `/api/channels/${state.activeChannelId}/messages`,
      { content, replyTo });
    if (!state.messages.some((m) => m.id === data.message.id)) {
      state.messages.push(data.message);
      state.atBottom = true;
      renderMessages();
      scrollToBottom();
    }
  } catch (err) {
    toast(err.message, 'error');
    input.value = content;
    autosize();
  }
}

function pushMessage(message) {
  if (state.messages.some((m) => m.id === message.id)) return;
  state.messages.push(message);
  state.atBottom = true;
  renderMessages();
  scrollToBottom();
}

// -------------------------------------------------------------- attachments

const MAX_UPLOAD_BYTES = 8 * 1024 * 1024;
const ALLOWED_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];

$('#attach-btn').onclick = () => $('#file-input').click();
$('#file-input').onchange = (e) => {
  uploadFiles([...e.target.files]);
  e.target.value = '';           // let the same file be picked again later
};

async function uploadFiles(files) {
  if (!state.activeChannelId) return;
  // The caption rides along with the first image only.
  let caption = input.value.trim();
  for (const file of files) {
    if (!ALLOWED_TYPES.includes(file.type)) {
      toast(`${file.name} isn't a PNG, JPEG, GIF or WebP.`, 'error');
      continue;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      toast(`${file.name} is ${formatBytes(file.size)} — the limit is 8 MB.`, 'error');
      continue;
    }
    if (caption) { input.value = ''; autosize(); }
    const params = new URLSearchParams({ filename: file.name });
    if (caption) params.set('caption', caption);
    caption = '';

    const chip = showUploading(file.name);
    try {
      const res = await fetch(
        `/api/channels/${state.activeChannelId}/upload?${params}`,
        { method: 'POST', body: file, credentials: 'same-origin',
          headers: { 'Content-Type': file.type } },
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `Upload failed (${res.status})`);
      pushMessage(data.message);
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      chip.remove();
    }
  }
}

function showUploading(name) {
  const chip = el('div', 'upload-chip');
  chip.append(el('span', 'mini-spinner'), el('span', null, `Uploading ${name}…`));
  $('#messages').appendChild(chip);
  scrollToBottom();
  return chip;
}

// Paste an image straight into the composer
input.addEventListener('paste', (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) {
    e.preventDefault();
    uploadFiles(files);
  }
});

// Drag and drop anywhere over the chat
(function enableDragDrop() {
  const chat = $('.chat');
  let depth = 0;
  const hint = () => $('#drop-hint');
  chat.addEventListener('dragenter', (e) => {
    if (![...(e.dataTransfer?.types || [])].includes('Files')) return;
    e.preventDefault();
    depth += 1;
    hint().classList.add('show');
  });
  chat.addEventListener('dragover', (e) => {
    if ([...(e.dataTransfer?.types || [])].includes('Files')) e.preventDefault();
  });
  chat.addEventListener('dragleave', () => {
    depth = Math.max(0, depth - 1);
    if (!depth) hint().classList.remove('show');
  });
  chat.addEventListener('drop', (e) => {
    if (![...(e.dataTransfer?.types || [])].includes('Files')) return;
    e.preventDefault();
    depth = 0;
    hint().classList.remove('show');
    uploadFiles([...e.dataTransfer.files]);
  });
})();

// ------------------------------------------------------------- ping sound

// Synthesised rather than shipped as a file: two short notes, no assets, no
// network request, and it can't be blocked by an ad blocker.
let audioCtx = null;

function pingSoundEnabled() {
  return localStorage.getItem('nexus.ping') !== 'off';
}

function setPingSound(on) {
  localStorage.setItem('nexus.ping', on ? 'on' : 'off');
}

function playPing() {
  if (!pingSoundEnabled()) return;
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    if (!audioCtx) audioCtx = new Ctx();
    // Browsers suspend audio until the page has been interacted with.
    if (audioCtx.state === 'suspended') audioCtx.resume();

    const now = audioCtx.currentTime;
    [[880, 0], [1174.66, 0.11]].forEach(([freq, offset]) => {
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = 'sine';
      osc.frequency.value = freq;
      // Quick fade in and out so it chimes instead of clicking.
      gain.gain.setValueAtTime(0, now + offset);
      gain.gain.linearRampToValueAtTime(0.16, now + offset + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + 0.22);
      osc.connect(gain).connect(audioCtx.destination);
      osc.start(now + offset);
      osc.stop(now + offset + 0.24);
    });
  } catch { /* audio is a nicety; never break the app over it */ }
}

// ------------------------------------------------------------ slash commands

const COIN = '💰';
const STARTING_COINS = 2000;      // matches db.STARTING_COINS

function coins(n) {
  return `${Number(n).toLocaleString()} ${COIN}`;
}

const COMMANDS = [
  {
    name: '/blackjack',
    args: 'cpu',
    summary: 'Play a hand against the Frontman',
    takesNumber: true,
    optionalNumber: true,
    run: (bet) => askStake('Blackjack vs the Frontman', bet, (n) => startGame('cpu', n)),
  },
  {
    name: '/blackjack',
    args: '1v1',
    summary: 'Open a 1v1 anyone in the server can join',
    takesNumber: true,
    optionalNumber: true,
    run: (bet) => askStake('Blackjack 1v1', bet, (n) => startGame('pvp', n)),
  },
  {
    name: '/poker',
    args: 'cpu',
    summary: "Hold'em against three house players",
    takesNumber: true,
    optionalNumber: true,
    run: (bet) => askStake('Poker vs the house', bet, (n) => startPoker(n, 3), 10),
  },
  {
    name: '/poker',
    args: 'table',
    summary: "Open a Hold'em table others can sit at",
    takesNumber: true,
    optionalNumber: true,
    run: (bet) => askStake('Poker table', bet, (n) => startPoker(n, 0), 10),
  },
  {
    name: '/slots',
    args: '<bet>',
    summary: 'Spin the slot machine',
    takesNumber: true,
    optionalNumber: true,
    run: (bet) => askStake('Slot machine', bet, spinSlots, 10),
  },
  {
    name: '/roulette',
    args: '<bet>',
    summary: 'Single-zero wheel — pick a colour, a number, anything',
    takesNumber: true,
    optionalNumber: true,
    run: (bet) => askRoulette(bet),
  },
  {
    name: '/poll',
    args: '<question>',
    summary: 'Ask everyone a question and count the votes',
    takesText: true,
    run: (question) => openPollBuilder(question),
  },
  {
    name: '/shop',
    args: '',
    summary: 'Spend Sana Coin on something permanent',
    run: () => openShop(),
  },
  {
    name: '/work',
    args: '',
    summary: 'Do a shift for Sana Coin, once an hour',
    run: () => doWork(),
  },
  {
    name: '/beg',
    args: '',
    summary: 'Ask the room for donations, once every 30 minutes',
    run: () => beg(),
  },
  {
    name: '/gif',
    args: '<search>',
    summary: 'Search Giphy and send a GIF',
    takesText: true,
    run: (term) => openGifPicker(term),
  },
  {
    name: '/claim',
    args: '',
    summary: `Collect your daily ${COIN} Sana Coin`,
    run: () => claimDaily(),
  },
  {
    name: '/balance',
    args: '',
    summary: 'Your balance, rank and record',
    run: () => showWallet(),
  },
  {
    name: '/bank',
    args: '',
    summary: "Everyone's balances in this server",
    run: () => showBank(),
  },
  {
    name: '/leaderboard',
    args: '',
    summary: 'Top players by games won',
    run: () => showLeaderboard(),
  },
  {
    name: '/purge',
    args: '<number>',
    summary: 'Delete the last N messages — server owner only',
    takesNumber: true,
    ownerOnly: true,
    run: (n) => confirmPurge(n),
  },
  {
    name: '/give',
    args: '<amount>',
    summary: 'Quietly hand someone Sana Coin — server owner only',
    takesNumber: true,
    optionalNumber: true,
    ownerOnly: true,
    run: (amount) => openGiveDialog(amount),
  },
  {
    name: '/reset',
    args: '',
    summary: 'Wipe balances and the leaderboard — server owner only',
    ownerOnly: true,
    run: () => confirmReset(),
  },
];

/** Owner-only commands are hidden from everyone else's command list. */
function commandAvailable(c) {
  if (!c.ownerOnly) return true;
  const guild = state.guilds.find((g) => g.id === state.activeGuildId);
  return !!(guild && guild.isOwner);
}

function availableCommands() {
  return COMMANDS.filter(commandAvailable);
}

function confirmPurge(count) {
  if (!count || count < 1) {
    toast('Say how many to delete, e.g. /purge 10', 'error');
    return;
  }
  const channel = state.activeChannel;
  if (!channel || channel.kind !== 'text') {
    toast('Purge only works in a server channel.', 'error');
    return;
  }
  // Destructive and irreversible, so never fire straight off the keystroke.
  confirmModal(
    `Delete the last ${count} message${count === 1 ? '' : 's'}?`,
    `The most recent ${count} message${count === 1 ? '' : 's'} in #${channel.name} `
    + 'will be permanently removed for everyone, along with any images in them. '
    + 'This cannot be undone.',
    `Delete ${count}`,
    async () => {
      const data = await api('POST', `/api/channels/${state.activeChannelId}/purge`,
        { count });
      await reloadChannelMessages();
      toast(`Deleted ${data.deleted} message${data.deleted === 1 ? '' : 's'}.`, 'ok');
    });
}

/** Owner-only, and nothing about it reaches the channel. */
async function openGiveDialog(preset) {
  const guild = state.guilds.find((g) => g.id === state.activeGuildId);
  if (!guild || !guild.isOwner) {
    toast('Only the owner of a server can do that.', 'error');
    return;
  }

  let members = [];
  try {
    const data = await api('GET', `/api/guilds/${guild.id}/members`);
    members = data.members.filter((m) => !m.isBot);
  } catch (err) { return toast(err.message, 'error'); }
  if (!members.length) return toast('Nobody to give anything to.', 'error');

  openModal((box, close) => {
    box.appendChild(el('h2', null, 'Give Sana Coin'));
    box.appendChild(el('p', 'sub',
      'Nothing is posted and nobody is told — the coins just turn up in their '
      + 'balance. A negative amount takes it back.'));

    const form = el('form');

    const whoLabel = el('label', 'field');
    whoLabel.appendChild(el('span', null, 'Who'));
    const who = el('select');
    members.forEach((m) => {
      const opt = el('option', null, `${m.username}#${m.discriminator}`);
      opt.value = String(m.id);
      who.appendChild(opt);
    });
    whoLabel.appendChild(who);
    form.appendChild(whoLabel);

    const amountLabel = el('label', 'field');
    amountLabel.appendChild(el('span', null, 'Amount'));
    const amount = el('input');
    amount.type = 'number';
    amount.value = String(preset != null ? preset : 10000);
    amountLabel.appendChild(amount);
    form.appendChild(amountLabel);

    const quick = el('div', 'stake-quick');
    [1000, 10000, 100000, 1000000].forEach((n) => {
      const b = el('button', 'btn small ghost', n.toLocaleString());
      b.type = 'button';
      b.onclick = () => { amount.value = String(n); };
      quick.appendChild(b);
    });
    const flip = el('button', 'btn small ghost', '±');
    flip.type = 'button';
    flip.title = 'Take it away instead';
    flip.onclick = () => { amount.value = String(-(parseInt(amount.value, 10) || 0)); };
    quick.appendChild(flip);
    form.appendChild(quick);

    const actions = el('div', 'modal-actions');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.type = 'button';
    cancel.onclick = close;
    const go = el('button', 'btn primary', 'Give');
    actions.append(cancel, go);
    form.appendChild(actions);

    form.onsubmit = async (e) => {
      e.preventDefault();
      const n = parseInt(amount.value, 10);
      if (!n) return toast('Enter an amount.', 'error');
      go.disabled = true;
      try {
        const data = await api('POST', `/api/guilds/${guild.id}/give`,
          { userId: Number(who.value), amount: n });
        close();
        const verb = data.amount > 0 ? 'Gave' : 'Took';
        toast(`${verb} ${Math.abs(data.moved).toLocaleString()} ${COIN} `
          + `${data.amount > 0 ? 'to' : 'from'} ${data.user.username}. `
          + `They now hold ${data.balance.toLocaleString()}.`, 'ok');
      } catch (err) { toast(err.message, 'error'); go.disabled = false; }
    };
    box.appendChild(form);
  });
}

function confirmReset() {
  const guild = state.guilds.find((g) => g.id === state.activeGuildId);
  if (!guild || !guild.isOwner) {
    toast('Only the owner of a server can reset it.', 'error');
    return;
  }
  confirmModal(
    `Reset ${guild.name}'s economy?`,
    `Everyone in ${guild.name} goes back to ${coins(STARTING_COINS)}, and every win, `
    + 'loss, streak and record is wiped. Sana Coin is one balance per account, '
    + 'so this resets what they hold in your other servers too. Perks they '
    + 'bought from the shop are kept. This cannot be undone.',
    'Reset everything',
    () => frontmanCard('reset'));
}

const ROULETTE_BETS = [
  { kind: 'red', label: 'Red', pays: '1:1', swatch: 'red' },
  { kind: 'black', label: 'Black', pays: '1:1', swatch: 'black' },
  { kind: 'odd', label: 'Odd', pays: '1:1' },
  { kind: 'even', label: 'Even', pays: '1:1' },
  { kind: 'low', label: '1–18', pays: '1:1' },
  { kind: 'high', label: '19–36', pays: '1:1' },
  { kind: 'dozen1', label: '1st dozen', pays: '2:1' },
  { kind: 'dozen2', label: '2nd dozen', pays: '2:1' },
  { kind: 'dozen3', label: '3rd dozen', pays: '2:1' },
  { kind: 'col1', label: '1st column', pays: '2:1' },
  { kind: 'col2', label: '2nd column', pays: '2:1' },
  { kind: 'col3', label: '3rd column', pays: '2:1' },
];

/** Pick what you're backing, then how much. */
async function askRoulette(preset, replace = null) {
  if (!state.activeChannelId) {
    toast('Open a channel first.', 'error');
    return;
  }
  const wallet = await refreshWallet();
  const have = wallet ? wallet.coins : 0;

  openModal((box, close) => {
    box.appendChild(el('h2', null, 'Roulette'));
    box.appendChild(el('p', 'sub',
      `Single zero, so the wheel has 37 pockets. You have ${coins(have)}.`));

    let chosen = 'red';
    let number = null;

    const grid = el('div', 'roulette-bets');
    const paint = () => {
      [...grid.querySelectorAll('button')].forEach((b) =>
        b.classList.toggle('on', b.dataset.kind === chosen));
    };
    ROULETTE_BETS.forEach((b) => {
      const cell = el('button', `roulette-bet ${b.swatch || ''}`.trim());
      cell.type = 'button';
      cell.dataset.kind = b.kind;
      cell.appendChild(el('span', 'rb-label', b.label));
      cell.appendChild(el('span', 'rb-pays', b.pays));
      cell.onclick = () => { chosen = b.kind; number = null; numField.value = ''; paint(); };
      grid.appendChild(cell);
    });
    box.appendChild(grid);

    const numLabel = el('label', 'field');
    numLabel.appendChild(el('span', null, 'Or straight up on a number — pays 35:1'));
    const numField = el('input');
    numField.type = 'number';
    numField.min = '0';
    numField.max = '36';
    numField.placeholder = '0 to 36';
    numField.oninput = () => {
      if (numField.value === '') { chosen = 'red'; number = null; }
      else { chosen = 'number'; number = parseInt(numField.value, 10); }
      paint();
    };
    numLabel.appendChild(numField);
    box.appendChild(numLabel);
    paint();

    const form = el('form');
    const stakeLabel = el('label', 'field');
    stakeLabel.appendChild(el('span', null, 'Stake'));
    const stake = el('input');
    stake.type = 'number';
    stake.min = '10';
    stake.max = String(have);
    stake.value = String(preset != null ? preset : Math.min(100, have));
    stakeLabel.appendChild(stake);
    stakeLabel.appendChild(el('small', null, 'Minimum 10 Sana Coin.'));
    form.appendChild(stakeLabel);

    const quick = el('div', 'stake-quick');
    [100, 500, 1000, 5000].filter((n) => n <= have).forEach((n) => {
      const b = el('button', 'btn small ghost', n.toLocaleString());
      b.type = 'button';
      b.onclick = () => { stake.value = String(n); };
      quick.appendChild(b);
    });
    form.appendChild(quick);

    const actions = el('div', 'modal-actions');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.type = 'button';
    cancel.onclick = close;
    const go = el('button', 'btn primary', 'Spin');
    actions.append(cancel, go);
    form.appendChild(actions);

    form.onsubmit = (e) => {
      e.preventDefault();
      const n = parseInt(stake.value, 10);
      if (Number.isNaN(n) || n < 10) return toast('Minimum spin is 10 Sana Coin.', 'error');
      if (n > have) return toast(`You only have ${coins(have)}.`, 'error');
      if (chosen === 'number' && !(number >= 0 && number <= 36)) {
        return toast('Pick a number from 0 to 36.', 'error');
      }
      close();
      spinRoulette(chosen, n, number, replace);
    };
    box.appendChild(form);
  });
}

async function spinRoulette(kind, bet, number, replace = null) {
  try {
    const data = await api('POST', `/api/channels/${state.activeChannelId}/roulette`,
      { kind, bet, number, replace });
    if (data.wallet) state.wallet = data.wallet;
    landCard(data.message, replace);
  } catch (err) { toast(err.message, 'error'); }
}

const doWork = () => frontmanCard('work');

async function beg() {
  if (!state.activeChannelId) {
    toast('Open a channel first.', 'error');
    return;
  }
  try {
    const data = await api('POST', `/api/channels/${state.activeChannelId}/beg`);
    if (data.wallet) state.wallet = data.wallet;
    pushMessage(data.message);
  } catch (err) { toast(err.message, 'error'); }
}

// ------------------------------------------------------------------- shop

async function openShop() {
  let data;
  try { data = await api('GET', '/api/shop'); }
  catch (err) { return toast(err.message, 'error'); }

  openModal((box, close) => {
    box.appendChild(el('h2', null, 'Sana Coin shop'));
    box.appendChild(el('p', 'sub',
      'The perks are permanent. The ticket and the hire are not. '
      + `You have ${coins(data.coins)}.`));

    const list = el('div', 'shop-list');
    data.items.forEach((item) => {
      const row = el('div', `shop-item ${item.owned ? 'owned' : ''}`.trim());
      row.appendChild(el('div', 'shop-icon', item.icon));

      const text = el('div', 'shop-text');
      text.appendChild(el('strong', null, item.name));
      text.appendChild(el('div', 'muted', item.summary));
      // The draw countdown belongs to the ticket, not to everything you can
      // buy more than once.
      text.appendChild(el('small', 'shop-detail', item.id === 'lottery'
        ? `${item.detail} Next draw in ${formatWait(data.drawIn)}.`
        : item.detail));
      if (item.held) {
        text.appendChild(el('small', 'shop-held',
          `You hold ${item.held} ticket${item.held === 1 ? '' : 's'} for it.`));
      }
      if (item.activeFor) {
        text.appendChild(el('small', 'shop-held',
          `On the job — ${formatWait(item.activeFor)} left. Hiring him again `
          + 'adds another day.'));
      }
      row.appendChild(text);

      const buy = el('button', `btn small ${item.owned ? 'ghost' : 'primary'}`,
        item.owned ? 'Owned' : coins(item.price));
      buy.disabled = item.owned || data.coins < item.price;
      if (!item.owned && data.coins < item.price) {
        buy.title = `${(item.price - data.coins).toLocaleString()} short`;
      }
      // Tickets are bought over and over, so the shop stays open and refreshes
      // in place; a perk is a one-off, and gets a confirmation.
      buy.onclick = item.repeatable
        ? () => buyTicket(item, close)
        : () => { close(); confirmBuy(item); };
      row.appendChild(buy);
      list.appendChild(row);
    });
    box.appendChild(list);
  });
}

async function buyTicket(item, close) {
  try {
    const data = await api('POST', '/api/shop/buy',
      { item: item.id, channelId: state.activeChannelId || undefined });
    if (data.wallet) state.wallet = data.wallet;
    if (data.message) pushMessage(data.message);
    if (item.hire) {
      const left = data.wallet && data.wallet.sawers ? data.wallet.sawers.activeFor : 0;
      toast(`${item.name} starts work — ${formatWait(left)} on the clock.`, 'ok');
    } else {
      const held = data.wallet && data.wallet.lottery ? data.wallet.lottery.tickets : 1;
      toast(`Ticket bought — you hold ${held} for the next draw.`, 'ok');
    }
    close();
    openShop();                       // reopen with the new count and balance
  } catch (err) { toast(err.message, 'error'); }
}

function confirmBuy(item) {
  confirmModal(
    `Buy the ${item.name.toLowerCase()}?`,
    `${item.summary} ${item.detail} It costs ${coins(item.price)}, and there `
    + 'are no refunds.',
    `Pay ${coins(item.price)}`,
    async () => {
      const data = await api('POST', '/api/shop/buy',
        { item: item.id, channelId: state.activeChannelId || undefined });
      if (data.wallet) state.wallet = data.wallet;
      if (data.message) pushMessage(data.message);
      // Perks change how names and channel lists are drawn everywhere.
      await refreshMe();
      await refreshAll();
      if (state.activeChannelId) renderMessages();
      toast(`${item.name} unlocked.`, 'ok');
    }, false);
}

async function refreshMe() {
  try {
    const data = await api('GET', '/api/me');
    if (data.user) state.me = data.user;
  } catch { /* the next poll will catch up */ }
}

// ------------------------------------------------------------------- polls

function openPollBuilder(question) {
  if (!state.activeChannelId) {
    toast('Open a channel first.', 'error');
    return;
  }
  openModal((box, close) => {
    box.appendChild(el('h2', null, 'Create a poll'));
    box.appendChild(el('p', 'sub',
      'Two options at least, six at most. Everyone in the channel can vote.'));

    const form = el('form');
    const qLabel = el('label', 'field');
    qLabel.appendChild(el('span', null, 'Question'));
    const qField = el('input');
    qField.maxLength = 200;
    qField.required = true;
    qField.placeholder = 'Where are we ordering from?';
    qField.value = question || '';
    qLabel.appendChild(qField);
    form.appendChild(qLabel);

    const options = el('div', 'poll-fields');
    form.appendChild(options);

    const addOption = (value = '') => {
      if (options.children.length >= 6) return;
      const wrap = el('div', 'poll-field');
      const field = el('input');
      field.maxLength = 80;
      field.placeholder = `Option ${options.children.length + 1}`;
      field.value = value;
      wrap.appendChild(field);
      const drop = el('button', 'icon-btn', '×');
      drop.type = 'button';
      drop.title = 'Remove';
      drop.onclick = () => {
        if (options.children.length <= 2) return;
        wrap.remove();
        more.disabled = false;
      };
      wrap.appendChild(drop);
      options.appendChild(wrap);
    };
    addOption();
    addOption();

    const more = el('button', 'btn small ghost', 'Add another option');
    more.type = 'button';
    more.onclick = () => {
      addOption();
      more.disabled = options.children.length >= 6;
    };
    form.appendChild(more);

    const multiLabel = el('label', 'field toggle-field');
    const multi = el('input');
    multi.type = 'checkbox';
    multiLabel.appendChild(multi);
    const multiText = el('div');
    multiText.appendChild(el('strong', null, 'Allow more than one answer'));
    multiText.appendChild(el('small', null,
      'Off, everyone gets one vote and changing it moves the old one.'));
    multiLabel.appendChild(multiText);
    form.appendChild(multiLabel);

    const actions = el('div', 'modal-actions');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.type = 'button';
    cancel.onclick = close;
    const go = el('button', 'btn primary', 'Post poll');
    actions.append(cancel, go);
    form.appendChild(actions);

    form.onsubmit = async (e) => {
      e.preventDefault();
      const labels = [...options.querySelectorAll('input')]
        .map((i) => i.value.trim()).filter(Boolean);
      if (labels.length < 2) return toast('A poll needs at least two options.', 'error');
      go.disabled = true;
      try {
        const data = await api('POST', `/api/channels/${state.activeChannelId}/polls`,
          { question: qField.value.trim(), options: labels, multi: multi.checked });
        pushMessage(data.message);
        close();
      } catch (err) { toast(err.message, 'error'); go.disabled = false; }
    };
    box.appendChild(form);
  });
}

async function refreshWallet() {
  try { state.wallet = await api('GET', '/api/wallet'); }
  catch { /* not signed in yet */ }
  return state.wallet;
}

/** "How much would you like to bet?" — skipped when the command carried one. */
async function askStake(title, preset, onConfirm, min = 0) {
  if (preset !== null && preset !== undefined) return onConfirm(preset);
  const wallet = await refreshWallet();
  const have = wallet ? wallet.coins : 0;

  openModal((box, close) => {
    box.appendChild(el('h2', null, title));
    box.appendChild(el('p', 'sub',
      `How much would you like to bet? You have ${coins(have)}.`));

    const form = el('form');
    const label = el('label', 'field');
    label.appendChild(el('span', null, 'Stake'));
    const field = el('input');
    field.type = 'number';
    field.min = String(min);
    field.max = String(have);
    field.value = String(Math.min(Math.max(min, 100), have));
    label.appendChild(field);
    if (min) label.appendChild(el('small', null, `Minimum ${coins(min)}.`));
    form.appendChild(label);

    const quick = el('div', 'stake-quick');
    [50, 100, 500, 1000].filter((n) => n >= min && n <= have).forEach((n) => {
      const b = el('button', 'btn small ghost', n.toLocaleString());
      b.type = 'button';
      b.onclick = () => { field.value = String(n); };
      quick.appendChild(b);
    });
    if (have >= min) {
      const allIn = el('button', 'btn small ghost', 'All in');
      allIn.type = 'button';
      allIn.onclick = () => { field.value = String(have); };
      quick.appendChild(allIn);
    }
    const none = el('button', 'btn small ghost', 'For fun');
    none.type = 'button';
    none.title = 'Play with nothing staked';
    none.onclick = () => { close(); onConfirm(0); };
    if (!min) quick.appendChild(none);
    form.appendChild(quick);

    const actions = el('div', 'modal-actions');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.type = 'button';
    cancel.onclick = close;
    const go = el('button', 'btn primary', 'Place bet');
    actions.append(cancel, go);
    form.appendChild(actions);

    form.onsubmit = (e) => {
      e.preventDefault();
      const n = parseInt(field.value, 10);
      if (Number.isNaN(n) || n < min) {
        toast(min ? `Minimum stake is ${coins(min)}.` : 'Enter a number.', 'error');
        return;
      }
      if (n > have) {
        toast(`You only have ${coins(have)}.`, 'error');
        return;
      }
      close();
      onConfirm(n);
    };
    box.appendChild(form);
  });
}

/** `replace` is the card a "Play again" was clicked on: the new hand takes
    it over instead of adding another card to the channel. */
async function startGame(mode, bet = 0, replace = null) {
  if (!state.activeChannelId) return;
  try {
    const data = await api('POST', `/api/channels/${state.activeChannelId}/games`,
      { mode, bet, replace });
    if (data.wallet) state.wallet = data.wallet;
    landCard(data.message, replace);
  } catch (err) { toast(err.message, 'error'); }
}

/** Swap the card in place when it was replayed, otherwise post a new one.
    replaceMessage already falls back to appending if the server declined to
    reuse the card and gave us a fresh message. */
function landCard(message, replace) {
  if (replace) replaceMessage(message);
  else pushMessage(message);
}

async function startPoker(bet, bots, replace = null) {
  if (!state.activeChannelId) return;
  try {
    const data = await api('POST', `/api/channels/${state.activeChannelId}/poker`,
      { bet, bots, replace });
    if (data.wallet) state.wallet = data.wallet;
    landCard(data.message, replace);
  } catch (err) { toast(err.message, 'error'); }
}

async function pokerCall(gameId, path, body) {
  try {
    const data = await api('POST', `/api/games/${gameId}/poker/${path}`, body);
    if (data.wallet) state.wallet = data.wallet;
    replaceMessage(data.message);
  } catch (err) { toast(err.message, 'error'); }
}

// ---------------------------------------------------------------- GIF picker

async function toggleFavourite(gif) {
  try {
    const res = await api('POST', '/api/gifs/favourites', {
      id: gif.id, url: gif.full || gif.url, preview: gif.preview, title: gif.title,
    });
    gif.favourite = res.favourite;
    return res.favourite;
  } catch (err) { toast(err.message, 'error'); return gif.favourite; }
}

async function openGifPicker(term) {
  if (!state.activeChannelId) {
    toast('Open a channel first.', 'error');
    return;
  }
  openModal((box, close) => {
    box.appendChild(el('h2', null, 'Send a GIF'));
    box.appendChild(el('p', 'sub', 'Powered by Giphy. Tap ★ to keep one.'));

    const tabs = el('div', 'tabs gif-tabs');
    const searchTab = el('button', 'tab active', 'Search');
    const favTab = el('button', 'tab', '★ Favourites');
    tabs.append(searchTab, favTab);
    box.appendChild(tabs);

    const form = el('form', 'inline-form');
    const field = el('input');
    field.placeholder = 'Search Giphy…';
    field.value = term || '';
    const go = el('button', 'btn primary', 'Search');
    form.append(field, go);
    box.appendChild(form);

    const grid = el('div', 'gif-grid');
    box.appendChild(grid);

    let token = 0;
    let favourites = new Set();

    function tile(g) {
      const cell = el('div', 'gif-choice');
      const img = el('img');
      img.src = g.preview;
      img.alt = g.title || 'GIF';
      img.loading = 'lazy';
      cell.appendChild(img);
      cell.title = g.title || 'GIF';

      const star = el('button', 'gif-star', '★');
      const paint = () => {
        const on = g.favourite || favourites.has(g.id);
        star.classList.toggle('on', !!on);
        star.title = on ? 'Remove from favourites' : 'Save to favourites';
      };
      paint();
      star.onclick = async (e) => {
        e.stopPropagation();
        const now = await toggleFavourite(g);
        if (now) favourites.add(g.id); else favourites.delete(g.id);
        paint();
      };
      cell.appendChild(star);

      cell.onclick = async () => { close(); await sendGif(g); };
      return cell;
    }

    async function render(loader, emptyText) {
      const mine = ++token;
      grid.replaceChildren(el('p', 'muted', 'Loading…'));
      try {
        const data = await loader();
        if (mine !== token) return;              // a newer request won
        grid.replaceChildren();
        if (!data.gifs.length) {
          grid.appendChild(el('p', 'muted', emptyText));
          return;
        }
        data.gifs.forEach((g) => grid.appendChild(tile(g)));
      } catch (err) {
        if (mine !== token) return;
        grid.replaceChildren(el('p', 'muted', err.message));
      }
    }

    async function loadFavourites() {
      try {
        const data = await api('GET', '/api/gifs/favourites');
        favourites = new Set(data.gifs.map((g) => g.id));
        return data;
      } catch { return { gifs: [] }; }
    }

    const showSearch = (q) => {
      searchTab.classList.add('active');
      favTab.classList.remove('active');
      form.hidden = false;
      render(async () => {
        await loadFavourites();
        return api('GET', `/api/gifs?q=${encodeURIComponent(q || '')}`);
      }, 'Nothing found. Try another search.');
    };
    const showFavourites = () => {
      favTab.classList.add('active');
      searchTab.classList.remove('active');
      form.hidden = true;
      render(loadFavourites, 'No favourites yet — tap ★ on a GIF to keep it.');
    };

    searchTab.onclick = () => showSearch(field.value.trim());
    favTab.onclick = showFavourites;
    form.onsubmit = (e) => { e.preventDefault(); showSearch(field.value.trim()); };
    showSearch(term || '');
  });
}

async function sendGif(gif) {
  // Sent as a message holding the URL; the renderer shows it inline.
  try {
    const data = await api('POST', `/api/channels/${state.activeChannelId}/messages`,
      { content: gif.full || gif.url });
    pushMessage(data.message);
  } catch (err) { toast(err.message, 'error'); }
}

async function spinSlots(bet, replace = null) {
  if (!state.activeChannelId) return;
  try {
    const data = await api('POST', `/api/channels/${state.activeChannelId}/slots`,
      { bet, replace });
    if (data.wallet) state.wallet = data.wallet;
    landCard(data.message, replace);
  } catch (err) { toast(err.message, 'error'); }
}

/** The Frontman answers these in-channel rather than in a local dialog. */
async function frontmanCard(kind) {
  if (!state.activeChannelId) {
    toast('Open a channel first.', 'error');
    return;
  }
  try {
    const data = await api('POST', `/api/channels/${state.activeChannelId}/frontman`,
      { kind });
    if (data.wallet) state.wallet = data.wallet;
    pushMessage(data.message);
  } catch (err) { toast(err.message, 'error'); }
}

const claimDaily = () => frontmanCard('claim');
const showWallet = () => frontmanCard('balance');
const showBank = () => frontmanCard('bank');
const showLeaderboard = () => frontmanCard('leaderboard');

function formatWait(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

/** Match typed text against a command; null when it isn't one. */
function matchCommand(text) {
  const trimmed = text.trim();
  if (!trimmed.startsWith('/')) return null;
  const [verbRaw, ...rest] = trimmed.split(/\s+/);
  const verb = verbRaw.toLowerCase();
  const tail = rest.join(' ').toLowerCase();

  const textual = COMMANDS.find((c) => c.name === verb && c.takesText);
  if (textual) return { cmd: textual, arg: rest.join(' ').trim() || null };

  // "/poker cpu 500" — variant word first, then the stake.
  const variant = COMMANDS.find(
    (c) => c.name === verb && c.takesNumber && c.args && !c.args.startsWith('<')
           && rest.length && rest[0].toLowerCase() === c.args);
  if (variant) {
    const n = parseInt(rest[1], 10);
    return { cmd: variant, arg: Number.isNaN(n) ? null : n };
  }

  const numeric = COMMANDS.find((c) => c.name === verb && c.takesNumber
                                       && (!c.args || c.args.startsWith('<')));
  if (numeric) {
    const n = parseInt(tail, 10);
    return { cmd: numeric, arg: Number.isNaN(n) ? null : n };
  }

  const exact = COMMANDS.find((c) => c.name === verb && c.args === tail);
  if (exact) return { cmd: exact };
  // "/blackjack" on its own runs the first variant.
  const bare = COMMANDS.find((c) => c.name === verb);
  if (bare && !tail) return { cmd: bare };
  return { unknown: verbRaw };
}

let commandBox = null;
let commandMatches = [];
let commandIndex = 0;

function closeCommands() {
  if (commandBox) { commandBox.remove(); commandBox = null; }
  commandMatches = [];
}

function renderCommandBox() {
  const text = input.value;
  // Only a leading slash opens the list, and only before a newline.
  if (!text.startsWith('/') || text.includes('\n')) return closeCommands();

  const q = text.toLowerCase();
  const verb = q.split(/\s+/)[0];
  const usable = availableCommands();
  // Prefer a full "name args" match; fall back to the verb so "/purge 10"
  // keeps showing the /purge hint while the number is being typed.
  let matches = usable.filter((c) => `${c.name} ${c.args}`.startsWith(q));
  if (!matches.length) matches = usable.filter((c) => c.name.startsWith(verb));
  if (!matches.length) return closeCommands();

  commandMatches = matches;
  commandIndex = Math.min(commandIndex, matches.length - 1);

  if (!commandBox) {
    commandBox = el('div', 'popover command-pop');
    document.body.appendChild(commandBox);
  }
  commandBox.replaceChildren();
  commandBox.appendChild(el('div', 'pop-title', 'Commands'));
  matches.forEach((c, i) => {
    const row = el('button', `mention-row ${i === commandIndex ? 'active' : ''}`.trim());
    row.appendChild(el('span', 'cmd-name', `${c.name} ${c.args}`));
    row.appendChild(el('span', 'mention-tag', c.summary));
    row.onmouseenter = () => { commandIndex = i; paintCommandSelection(); };
    row.onclick = () => {
      const typed = matchCommand(input.value);
      if (typed && typed.cmd === c && typed.arg != null) runCommand(c, typed.arg);
      else runCommand(c);
    };
    commandBox.appendChild(row);
  });

  const box = $('#composer-box').getBoundingClientRect();
  commandBox.style.left = `${box.left}px`;
  commandBox.style.width = `${Math.min(420, box.width)}px`;
  const rect = commandBox.getBoundingClientRect();
  commandBox.style.top = `${Math.max(12, box.top - rect.height - 8)}px`;
}

function paintCommandSelection() {
  if (!commandBox) return;
  [...commandBox.querySelectorAll('.mention-row')].forEach((r, i) =>
    r.classList.toggle('active', i === commandIndex));
}

function runCommand(cmd, arg) {
  // A command that needs a number and hasn't got one yet: prefill the box and
  // let them finish typing rather than firing with nothing. Commands where the
  // number is optional (the stake) ask for it in a dialog instead.
  if (cmd.takesNumber && !cmd.optionalNumber && (arg === null || arg === undefined)) {
    input.value = `${cmd.name} `;
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    autosize();
    renderCommandBox();
    return;
  }
  input.value = '';
  autosize();
  closeCommands();
  cmd.run(arg);
}

input.addEventListener('input', renderCommandBox);

// --------------------------------------------------------- @ mention autocomplete

let mentionBox = null;
let mentionMatches = [];
let mentionIndex = 0;
let mentionStart = -1;

function closeMentions() {
  if (mentionBox) { mentionBox.remove(); mentionBox = null; }
  mentionMatches = [];
  mentionStart = -1;
}

/** The "@word" being typed immediately before the caret, or null. */
function activeMentionQuery() {
  const caret = input.selectionStart ?? 0;
  const before = input.value.slice(0, caret);
  // Allow spaces inside the query so multi-word usernames can be found, but
  // stop at a newline or a preceding non-boundary character.
  const at = before.lastIndexOf('@');
  if (at === -1) return null;
  if (at > 0 && !/[\s(]/.test(before[at - 1])) return null;
  const query = before.slice(at + 1);
  if (/[\n`]/.test(query) || query.length > 32) return null;
  // A '#' can only come from a finished tag — usernames never contain one —
  // so this mention is already complete and needs no suggestions.
  if (/#\d{0,4}/.test(query)) return null;
  return { at, query };
}

function renderMentionBox() {
  const found = activeMentionQuery();
  if (!found || !state.activeChannelId) return closeMentions();

  const q = found.query.toLowerCase().trim();
  const people = state.mentionable
    .filter((u) => !state.me || u.id !== state.me.id)
    .filter((u) => u.username.toLowerCase().includes(q) || u.tag.toLowerCase().includes(q));

  const options = [];
  if (state.mentionEveryone && 'everyone'.startsWith(q)) {
    options.push({ everyone: true, username: 'everyone',
                   note: 'Notify everyone in this server' });
  }
  options.push(...people.slice(0, 8));

  if (!options.length) return closeMentions();

  mentionMatches = options;
  mentionStart = found.at;
  mentionIndex = Math.min(mentionIndex, options.length - 1);

  if (!mentionBox) {
    mentionBox = el('div', 'popover mention-pop');
    document.body.appendChild(mentionBox);
  }
  mentionBox.replaceChildren();
  mentionBox.appendChild(el('div', 'pop-title', 'Members'));

  options.forEach((u, i) => {
    const row = el('button', `mention-row ${i === mentionIndex ? 'active' : ''}`.trim());
    if (u.everyone) {
      row.appendChild(el('span', 'mention-everyone', '@'));
      row.appendChild(el('span', 'mention-name', 'everyone'));
      row.appendChild(el('span', 'mention-tag', u.note));
    } else {
      row.appendChild(avatar(u, 'xs'));
      row.appendChild(el('span', 'mention-name', u.username));
      row.appendChild(el('span', 'mention-tag', `#${u.discriminator}`));
    }
    row.onmouseenter = () => { mentionIndex = i; paintMentionSelection(); };
    row.onclick = () => applyMention(u);
    mentionBox.appendChild(row);
  });

  // Anchor above the composer, aligned to its left edge.
  const box = $('#composer-box').getBoundingClientRect();
  mentionBox.style.left = `${box.left}px`;
  const rect = mentionBox.getBoundingClientRect();
  mentionBox.style.top = `${Math.max(12, box.top - rect.height - 8)}px`;
  mentionBox.style.width = `${Math.min(360, box.width)}px`;
}

function paintMentionSelection() {
  if (!mentionBox) return;
  [...mentionBox.querySelectorAll('.mention-row')].forEach((r, i) =>
    r.classList.toggle('active', i === mentionIndex));
}

function applyMention(u) {
  if (mentionStart < 0) return;
  const caret = input.selectionStart ?? input.value.length;
  const token = u.everyone ? '@everyone ' : `@${u.username}#${u.discriminator} `;
  input.value = input.value.slice(0, mentionStart) + token + input.value.slice(caret);
  const pos = mentionStart + token.length;
  input.focus();
  input.setSelectionRange(pos, pos);
  closeMentions();
  autosize();
}

input.addEventListener('input', renderMentionBox);
input.addEventListener('blur', () => setTimeout(closeMentions, 120));

async function loadMentionable(channelId) {
  state.mentionable = [];
  state.mentionEveryone = false;
  try {
    const data = await api('GET', `/api/channels/${channelId}/mentionable`);
    if (state.activeChannelId !== channelId) return;
    state.mentionable = data.users;
    state.mentionEveryone = data.everyone;
  } catch { /* autocomplete is a convenience, not required */ }
}

// ----------------------------------------------------------- emoji & stickers

$('#emoji-btn').onclick = (e) => {
  openEmojiPicker(e.currentTarget, (emoji) => {
    const at = input.selectionStart ?? input.value.length;
    input.value = input.value.slice(0, at) + emoji + input.value.slice(at);
    input.focus();
    input.setSelectionRange(at + emoji.length, at + emoji.length);
    autosize();
  });
};

$('#gif-btn').onclick = () => openGifPicker('');
$('#sticker-btn').onclick = (e) => openStickerPicker(e.currentTarget);

async function stickerSource() {
  // In a server channel use that server's stickers; in a DM, offer every
  // sticker from the servers you're in.
  if (state.activeGuildId) return [state.activeGuildId];
  return state.guilds.map((g) => g.id);
}

async function openStickerPicker(anchor) {
  closePopover();
  anchor.setAttribute('data-popover-anchor', '');
  const pop = el('div', 'popover sticker-pop');
  pop.appendChild(el('div', 'pop-title', 'Stickers'));
  const grid = el('div', 'sticker-grid');
  pop.appendChild(grid);
  grid.appendChild(el('p', 'muted', 'Loading…'));
  openPopover = pop;
  placePopover(pop, anchor);

  const guildIds = await stickerSource();
  const all = [];
  for (const gid of guildIds) {
    try {
      const data = await api('GET', `/api/guilds/${gid}/stickers`);
      data.stickers.forEach((s) => all.push({ ...s, guildId: gid }));
    } catch { /* skip servers we can't read */ }
  }

  grid.replaceChildren();
  if (!all.length) {
    grid.appendChild(el('p', 'muted',
      state.activeGuildId
        ? 'No stickers yet. The server owner can add them from the server menu.'
        : 'No stickers yet — add some in one of your servers.'));
  }
  all.forEach((s) => {
    const b = el('button', 'sticker-choice');
    const img = el('img');
    img.src = s.url;
    img.alt = s.name;
    b.appendChild(img);
    b.title = s.name;
    b.onclick = async () => {
      closePopover();
      try {
        const data = await api('POST', `/api/channels/${state.activeChannelId}/messages`,
          { stickerId: s.id });
        pushMessage(data.message);
      } catch (err) { toast(err.message, 'error'); }
    };
    grid.appendChild(b);
  });
  placePopover(pop, anchor);
}

// ------------------------------------------------------------------ polling

function stopPoll() {
  if (state.pollAbort) { state.pollAbort.abort(); state.pollAbort = null; }
}

async function startPoll() {
  stopPoll();
  const controller = new AbortController();
  state.pollAbort = controller;
  const channelAtStart = state.activeChannelId;

  while (!controller.signal.aborted) {
    const after = state.messages.length
      ? state.messages[state.messages.length - 1].id : 0;
    const params = new URLSearchParams({ rev: state.rev, after: String(after) });
    if (state.activeChannelId) {
      params.set('channel', String(state.activeChannelId));
      params.set('crev', state.channelRev || '');
    }

    try {
      const res = await fetch(`/api/poll?${params}`, {
        signal: controller.signal,
        credentials: 'same-origin',
      });
      if (controller.signal.aborted || state.activeChannelId !== channelAtStart) return;
      if (res.status === 401) {
        // Don't kick the user out on a blip — confirm we're really signed out.
        if (await stillSignedIn()) { await sleep(2000); continue; }
        showAuth();
        toast('Your session ended. Please sign in again.', 'error');
        return;
      }
      if (!res.ok) { await sleep(3000); continue; }
      const data = await res.json();

      if (data.messages && data.messages.length) {
        const known = new Set(state.messages.map((m) => m.id));
        const fresh = data.messages.filter((m) => !known.has(m.id));
        if (fresh.length) {
          // Chime once per batch, not once per message — and not at all from
          // a channel that has been muted.
          if (!activeChannelMuted()
              && fresh.some((m) => m.mentionsMe && m.author.id !== state.me.id)) {
            playPing();
          }
          state.messages.push(...fresh);
          renderMessages();
          if (state.atBottom) scrollToBottom();
        }
      }
      // Reactions, edits and deletions don't arrive as new messages, so when
      // the channel fingerprint moves we redraw the visible window.
      if (data.channelChanged && state.activeChannelId === channelAtStart) {
        const wasRev = state.channelRev;
        state.channelRev = data.channelRev;
        if (wasRev) await reloadChannelMessages();
      } else if (data.channelRev) {
        state.channelRev = data.channelRev;
      }

      const changed = data.rev !== state.rev;
      state.rev = data.rev;
      if (changed) {
        // A mention in a channel you're not looking at never reaches the
        // message stream, so watch the unread-ping totals instead.
        const pingsBefore = totalMentions();
        await refreshSidebarData();
        if (totalMentions() > pingsBefore) playPing();
        renderRail();
        renderSidebar();
        if (!state.activeChannelId) renderFriendsView();
        else if (state.activeChannel && state.activeChannel.kind === 'text') renderMembers();
      }
    } catch (err) {
      if (controller.signal.aborted) return;
      await sleep(2500);
    }
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Redraw the current channel in place, keeping the scroll position. */
async function reloadChannelMessages() {
  const channelId = state.activeChannelId;
  if (!channelId) return;
  try {
    const data = await api('GET', `/api/channels/${channelId}/messages`);
    if (state.activeChannelId !== channelId) return;
    const box = $('#messages');
    const wasAtBottom = state.atBottom;
    const offset = box.scrollHeight - box.scrollTop;
    state.messages = data.messages;
    renderMessages();
    if (wasAtBottom) scrollToBottom();
    else box.scrollTop = box.scrollHeight - offset;
  } catch { /* the next poll will try again */ }
}

/** Second opinion before tearing the UI down: is the session actually dead? */
async function stillSignedIn() {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const res = await fetch('/api/me', { credentials: 'same-origin' });
      if (res.ok) {
        const data = await res.json();
        if (data.user) { state.me = data.user; return true; }
        return false;                 // server is up and says we're anonymous
      }
    } catch { /* network hiccup — try once more */ }
    await sleep(1500);
  }
  return true;                        // couldn't reach the server; stay put
}

// ------------------------------------------------------------------ data

/** Unread pings across every unmuted server channel, and every DM. */
function totalMentions() {
  const inGuilds = state.guilds.reduce(
    (n, g) => n + g.channels.reduce(
      (c, ch) => c + (ch.muted ? 0 : (ch.mentions || 0)), 0), 0);
  const inDms = state.dms.reduce((n, d) => n + (d.mentions || 0), 0);
  return inGuilds + inDms;
}

/** Is the channel currently on screen muted? */
function activeChannelMuted() {
  return state.guilds.some((g) => g.channels.some(
    (c) => c.id === state.activeChannelId && c.muted));
}

async function refreshSidebarData() {
  const [guilds, dms, friends, wallet] = await Promise.all([
    api('GET', '/api/guilds'),
    api('GET', '/api/dms'),
    api('GET', '/api/friends'),
    api('GET', '/api/wallet').catch(() => null),
  ]);
  if (wallet) {
    state.wallet = wallet;
    announceSawers(wallet.sawersEarnings);
    announceLottery(wallet.lotteryResults);
  }
  state.guilds = guilds.guilds;
  state.dms = dms.dms;
  state.friends = friends;
}

/** What E. Sawers brought back while you were away. Handed over once. */
function announceSawers(earnings) {
  if (!earnings || !earnings.hours) return;
  const h = earnings.hours;
  toast(`🧑‍🏭 E. Sawers worked ${h} hour${h === 1 ? '' : 's'} — `
    + `${coins(earnings.coins)}.${earnings.finished ? " That's him done." : ''}`, 'ok');
}

/** Your own half of the draw. The server hands each result over once. */
function announceLottery(result) {
  if (!result || !result.tickets) return;
  if (result.won) {
    toast(`🎟 Your lottery ticket came up — ${coins(result.won)}!`, 'ok');
    return;
  }
  const n = result.tickets;
  toast(`🎟 No luck in the draw — ${n} ticket${n === 1 ? '' : 's'} went down.`);
}

async function refreshAll() {
  await refreshSidebarData();
  renderRail();
  renderSidebar();
  if (!state.activeChannelId) renderFriendsView();
}

// ------------------------------------------------------------------ servers

$('#rail-add').onclick = () => {
  openModal((box, close) => {
    box.appendChild(el('h2', null, 'Create a server'));
    box.appendChild(el('p', 'sub',
      'Your server is where you and your friends hang out. It starts with #general and #random.'));
    const form = el('form');
    const label = el('label', 'field');
    label.appendChild(el('span', null, 'Server name'));
    const field = el('input');
    field.maxLength = 60;
    field.required = true;
    field.placeholder = "Alex's hangout";
    label.appendChild(field);
    form.appendChild(label);
    const actions = el('div', 'modal-actions');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.type = 'button';
    cancel.onclick = close;
    const go = el('button', 'btn primary', 'Create');
    go.type = 'submit';
    actions.append(cancel, go);
    form.appendChild(actions);
    form.onsubmit = async (e) => {
      e.preventDefault();
      go.disabled = true;
      try {
        const data = await api('POST', '/api/guilds', { name: field.value.trim() });
        await refreshSidebarData();
        close();
        state.activeGuildId = data.guildId;
        await openChannel(data.channelId);
        toast('Server created.', 'ok');
      } catch (err) { toast(err.message, 'error'); go.disabled = false; }
    };
    box.appendChild(form);
  });
};

$('#rail-join').onclick = (prefill = '') => {
  openModal((box, close) => {
    box.appendChild(el('h2', null, 'Join a server'));
    box.appendChild(el('p', 'sub',
      'Paste an invite code someone sent you. Codes look like kR7pQ2mn.'));
    const form = el('form');
    const label = el('label', 'field');
    label.appendChild(el('span', null, 'Invite code or link'));
    const field = el('input');
    field.required = true;
    field.placeholder = 'kR7pQ2mn';
    if (typeof prefill === 'string') field.value = prefill;
    label.appendChild(field);
    form.appendChild(label);
    const preview = el('div', 'muted');
    form.appendChild(preview);
    const actions = el('div', 'modal-actions');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.type = 'button';
    cancel.onclick = close;
    const go = el('button', 'btn primary', 'Join');
    go.type = 'submit';
    actions.append(cancel, go);
    form.appendChild(actions);
    form.onsubmit = async (e) => {
      e.preventDefault();
      const code = extractCode(field.value);
      if (!code) return;
      go.disabled = true;
      try {
        const data = await api('POST', `/api/invites/${code}/join`);
        await refreshSidebarData();
        close();
        if (data.channelId) {
          state.activeGuildId = data.guildId;
          await openChannel(data.channelId);
        }
        toast(data.alreadyMember ? 'You were already in that server.'
          : 'Joined the server.', 'ok');
        history.replaceState({}, '', '/');
      } catch (err) { toast(err.message, 'error'); go.disabled = false; }
    };
    box.appendChild(form);
  });
};

function extractCode(raw) {
  const value = String(raw || '').trim();
  const match = value.match(/([A-Za-z0-9]{8})\s*$/);
  return match ? match[1] : value.replace(/[^A-Za-z0-9]/g, '');
}

$('#guild-menu-btn').onclick = () => {
  const guild = state.guilds.find((g) => g.id === state.activeGuildId);
  if (!guild) return toast('Open a server to see its options.', 'error');
  openModal((box, close) => {
    box.appendChild(el('h2', null, guild.name));
    box.appendChild(el('p', 'sub',
      `${guild.memberCount} member${guild.memberCount === 1 ? '' : 's'} · ` +
      `${guild.isOwner ? 'You own this server' : 'You are a member'}`));

    const stack = el('div');
    stack.style.cssText = 'display:flex;flex-direction:column;gap:8px';

    const inviteBtn = el('button', 'btn primary', 'Create an invite code');
    inviteBtn.onclick = () => { close(); showInvites(guild.id); };
    stack.appendChild(inviteBtn);

    if (guild.isOwner) {
      const chan = el('button', 'btn', 'Create a channel');
      chan.onclick = () => { close(); promptCreateChannel(guild.id); };
      stack.appendChild(chan);

      const bot = el('button', 'btn', 'Frontman usage');
      bot.onclick = () => { close(); frontmanUsage(guild.id); };
      stack.appendChild(bot);

      const chanSettings = el('button', 'btn', 'Channel settings');
      chanSettings.onclick = () => { close(); channelSettings(guild.id); };
      stack.appendChild(chanSettings);

      const stick = el('button', 'btn', 'Manage stickers');
      stick.onclick = () => { close(); manageStickers(guild.id); };
      stack.appendChild(stick);

      const icon = el('button', 'btn', 'Change server icon');
      icon.onclick = () => { close(); promptServerIcon(guild); };
      stack.appendChild(icon);

      const rename = el('button', 'btn', 'Rename server');
      rename.onclick = () => { close(); promptRename(guild); };
      stack.appendChild(rename);

      const del = el('button', 'btn danger', 'Delete server');
      del.onclick = () => {
        close();
        confirmModal('Delete server?',
          `${guild.name}, its channels and every message in it will be gone for good.`,
          'Delete server', async () => {
            await api('DELETE', `/api/guilds/${guild.id}`);
            state.activeGuildId = null;
            state.activeChannelId = null;
            await refreshSidebarData();
            openFriends();
            toast('Server deleted.');
          });
      };
      stack.appendChild(del);
    } else {
      const leave = el('button', 'btn danger', 'Leave server');
      leave.onclick = () => {
        close();
        confirmModal('Leave server?',
          `You'll need a new invite to get back into ${guild.name}.`,
          'Leave', async () => {
            await api('DELETE', `/api/guilds/${guild.id}`);
            state.activeGuildId = null;
            state.activeChannelId = null;
            await refreshSidebarData();
            openFriends();
          });
      };
      stack.appendChild(leave);
    }
    box.appendChild(stack);
  });
};

// Left to right: no limit through to off entirely. -1 is off duty.
const BOT_STEPS = [
  [0, 'Instantly', 'No limit at all.'],
  [5, 'Every 5 seconds', 'Barely a pause.'],
  [10, 'Every 10 seconds', 'Enough to stop double-clicks.'],
  [30, 'Every 30 seconds', 'A breather between hands.'],
  [60, 'Every minute', 'Keeps a channel readable.'],
  [120, 'Every 2 minutes', 'Games become an occasional thing.'],
  [300, 'Every 5 minutes', 'A few hands an hour.'],
  [600, 'Every 10 minutes', 'Rationed.'],
  [900, 'Every 15 minutes', 'Rare.'],
  [1800, 'Every 30 minutes', 'Twice an hour, at most.'],
  [3600, 'Once an hour', 'Strictly special occasions.'],
  [-1, 'Not at this time', 'The Frontman is off duty. No games, no cards.'],
];

/** How often members may call on the Frontman, server-wide. */
function frontmanUsage(guildId) {
  const guild = state.guilds.find((g) => g.id === guildId);
  if (!guild) return;
  let index = Math.max(0, BOT_STEPS.findIndex(
    ([secs]) => secs === (guild.frontmanCooldown || 0)));

  openModal((box, close) => {
    box.appendChild(el('h2', null, 'Frontman usage'));
    box.appendChild(el('p', 'sub',
      'How often each member can call on the Frontman in this server. You are '
      + 'exempt, and it applies to every channel at once.'));

    const readout = el('div', 'slider-readout');
    const value = el('strong');
    const hint = el('span', 'muted');
    readout.append(value, hint);
    box.appendChild(readout);

    const slider = el('input');
    slider.type = 'range';
    slider.min = '0';
    slider.max = String(BOT_STEPS.length - 1);
    slider.step = '1';
    slider.value = String(index);
    slider.className = 'slider';
    box.appendChild(slider);

    const ends = el('div', 'slider-ends');
    ends.append(el('span', null, 'No limit'), el('span', null, 'Off duty'));
    box.appendChild(ends);

    const paint = () => {
      const [, label, note] = BOT_STEPS[Number(slider.value)];
      value.textContent = label;
      hint.textContent = note;
      readout.classList.toggle('off', BOT_STEPS[Number(slider.value)][0] === -1);
    };
    slider.oninput = paint;
    paint();

    const actions = el('div', 'modal-actions');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = close;
    const save = el('button', 'btn primary', 'Save');
    save.onclick = async () => {
      save.disabled = true;
      const [secs, label] = BOT_STEPS[Number(slider.value)];
      try {
        await api('PATCH', `/api/guilds/${guildId}/settings`,
          { frontmanCooldown: secs });
        guild.frontmanCooldown = secs;
        close();
        toast(`Frontman usage: ${label.toLowerCase()}.`, 'ok');
      } catch (err) { toast(err.message, 'error'); save.disabled = false; }
    };
    actions.append(cancel, save);
    box.appendChild(actions);
  });
}

// Seconds, and what to call them. Matches the ladder the server accepts.
const SLOW_STEPS = [
  [0, 'Off'], [5, '5 seconds'], [10, '10 seconds'], [15, '15 seconds'],
  [30, '30 seconds'], [60, '1 minute'], [120, '2 minutes'], [300, '5 minutes'],
  [600, '10 minutes'], [900, '15 minutes'], [1800, '30 minutes'],
  [3600, '1 hour'], [21600, '6 hours'],
];

/** Per-channel rules, owner only. One channel at a time. */
function channelSettings(guildId, channelId) {
  const guild = state.guilds.find((g) => g.id === guildId);
  if (!guild) return;
  // The lounge is bought, not administered; leave it out of the list.
  const channels = guild.channels.filter((c) => !c.locked);
  if (!channels.length) return toast('No channels to configure.', 'error');
  let current = channels.find((c) => c.id === channelId) || channels[0];

  openModal((box, close) => {
    box.appendChild(el('h2', null, 'Channel settings'));
    box.appendChild(el('p', 'sub',
      'Rules for one channel at a time. Everything here is off by default.'));

    const pick = el('label', 'field');
    pick.appendChild(el('span', null, 'Channel'));
    const select = el('select');
    channels.forEach((c) => {
      const opt = el('option', null, `#${c.name}`);
      opt.value = String(c.id);
      if (c.id === current.id) opt.selected = true;
      select.appendChild(opt);
    });
    pick.appendChild(select);
    box.appendChild(pick);

    const panel = el('div');
    box.appendChild(panel);

    const save = async (patch) => {
      try {
        const data = await api('PATCH', `/api/channels/${current.id}/settings`, patch);
        Object.assign(current, data.channel);
        // The open channel's own copy drives the composer hint.
        if (state.activeChannel && state.activeChannel.id === current.id) {
          Object.assign(state.activeChannel, data.channel);
          paintChannelRules();
        }
        return true;
      } catch (err) { toast(err.message, 'error'); return false; }
    };

    const draw = () => {
      panel.replaceChildren();

      const slow = el('label', 'field');
      slow.appendChild(el('span', null, 'Slow mode'));
      const slowSelect = el('select');
      SLOW_STEPS.forEach(([secs, label]) => {
        const opt = el('option', null, label);
        opt.value = String(secs);
        if (secs === (current.slowMode || 0)) opt.selected = true;
        slowSelect.appendChild(opt);
      });
      slowSelect.onchange = async () => {
        const secs = Number(slowSelect.value);
        if (await save({ slowMode: secs })) {
          toast(secs ? `Slow mode: one message every ${SLOW_STEPS
            .find(([s]) => s === secs)[1].toLowerCase()}.` : 'Slow mode off.', 'ok');
        }
      };
      slow.appendChild(slowSelect);
      slow.appendChild(el('small', null,
        'How long a member waits between messages. You are exempt.'));
      panel.appendChild(slow);

      panel.appendChild(toggleRow(
        'Polls only', current.pollsOnly,
        'Nothing but polls can be posted. Applies to you too — turn it off to talk.',
        (on) => save({ pollsOnly: on })));

      panel.appendChild(toggleRow(
        'No bots', current.noBots,
        'Keeps the Frontman out, so no games and no cards in here.',
        (on) => save({ noBots: on })));
    };

    select.onchange = () => {
      current = channels.find((c) => c.id === Number(select.value));
      draw();
    };
    draw();

    const actions = el('div', 'modal-actions');
    const done = el('button', 'btn primary', 'Done');
    done.onclick = () => { close(); refreshAll(); };
    actions.appendChild(done);
    box.appendChild(actions);
  });
}

/** A labelled switch that saves the moment it is flipped. */
function toggleRow(title, on, hint, onChange) {
  const label = el('label', 'field toggle-field');
  const box = el('input');
  box.type = 'checkbox';
  box.checked = !!on;
  box.onchange = async () => {
    const ok = await onChange(box.checked);
    if (!ok) box.checked = !box.checked;
  };
  label.appendChild(box);
  const text = el('div');
  text.appendChild(el('strong', null, title));
  text.appendChild(el('small', null, hint));
  label.appendChild(text);
  return label;
}

function promptServerIcon(guild) {
  openModal((box, close) => {
    box.appendChild(el('h2', null, 'Server icon'));
    box.appendChild(el('p', 'sub',
      'PNG, JPEG, GIF or WebP up to 2 MB. Square images look best.'));

    const row = el('div', 'pic-row');
    let preview = serverIconPreview(guild);
    row.appendChild(preview);

    const buttons = el('div', 'pic-buttons');
    const file = el('input');
    file.type = 'file';
    file.accept = 'image/png,image/jpeg,image/gif,image/webp';
    file.hidden = true;
    const upload = el('button', 'btn small', 'Upload image');
    upload.type = 'button';
    upload.onclick = () => file.click();
    buttons.append(upload, file);

    const remove = el('button', 'btn small ghost', 'Remove');
    remove.type = 'button';
    remove.hidden = !guild.iconUrl;
    buttons.appendChild(remove);

    const status = el('small', 'muted');
    buttons.appendChild(status);
    row.appendChild(buttons);
    box.appendChild(row);

    const refresh = () => {
      const fresh = serverIconPreview(guild);
      preview.replaceWith(fresh);
      preview = fresh;
      remove.hidden = !guild.iconUrl;
      renderRail();
    };

    file.onchange = async () => {
      const chosen = file.files[0];
      file.value = '';
      if (!chosen) return;
      if (chosen.size > 2 * 1024 * 1024) {
        status.textContent = `That image is ${formatBytes(chosen.size)} — the limit is 2 MB.`;
        return;
      }
      status.textContent = 'Uploading…';
      try {
        const res = await fetch(`/api/guilds/${guild.id}/icon`, {
          method: 'POST', body: chosen, credentials: 'same-origin',
          headers: { 'Content-Type': chosen.type },
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `Upload failed (${res.status})`);
        guild.iconUrl = data.iconUrl;
        status.textContent = '';
        refresh();
        await refreshSidebarData();
        renderRail();
        toast('Server icon updated.', 'ok');
      } catch (err) { status.textContent = err.message; }
    };

    remove.onclick = async () => {
      try {
        await api('DELETE', `/api/guilds/${guild.id}/icon`);
        guild.iconUrl = null;
        refresh();
        await refreshSidebarData();
        renderRail();
      } catch (err) { toast(err.message, 'error'); }
    };

    const done = el('div', 'modal-actions');
    const ok = el('button', 'btn primary', 'Done');
    ok.onclick = close;
    done.appendChild(ok);
    box.appendChild(done);
  });
}

function serverIconPreview(guild) {
  const node = el('div', 'avatar xl server-icon-preview');
  if (guild.iconUrl) {
    const img = el('img');
    img.src = guild.iconUrl;
    img.alt = '';
    node.appendChild(img);
  } else {
    node.textContent = guildInitials(guild.name);
    node.style.background = guild.color;
  }
  return node;
}

function promptRename(guild) {
  openModal((box, close) => {
    box.appendChild(el('h2', null, 'Rename server'));
    const form = el('form');
    const label = el('label', 'field');
    label.appendChild(el('span', null, 'Server name'));
    const field = el('input');
    field.value = guild.name;
    field.maxLength = 60;
    field.required = true;
    label.appendChild(field);
    form.appendChild(label);
    const actions = el('div', 'modal-actions');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.type = 'button';
    cancel.onclick = close;
    const go = el('button', 'btn primary', 'Save');
    actions.append(cancel, go);
    form.appendChild(actions);
    form.onsubmit = async (e) => {
      e.preventDefault();
      try {
        await api('PATCH', `/api/guilds/${guild.id}`, { name: field.value.trim() });
        await refreshAll();
        close();
      } catch (err) { toast(err.message, 'error'); }
    };
    box.appendChild(form);
  });
}

function manageStickers(guildId) {
  openModal((box) => {
    const guild = state.guilds.find((g) => g.id === guildId);
    box.appendChild(el('h2', null, 'Stickers'));
    box.appendChild(el('p', 'sub',
      `Custom stickers for ${guild ? guild.name : 'this server'}. `
      + 'Anyone in the server can send them. PNG, JPEG, GIF or WebP, up to 1 MB.'));

    const form = el('form', 'inline-form');
    const nameInput = el('input');
    nameInput.placeholder = 'sticker-name';
    nameInput.maxLength = 24;
    nameInput.required = true;
    const pick = el('button', 'btn', 'Choose image');
    pick.type = 'button';
    const file = el('input');
    file.type = 'file';
    file.accept = 'image/png,image/jpeg,image/gif,image/webp';
    file.hidden = true;
    pick.onclick = () => file.click();
    form.append(nameInput, pick, file);
    box.appendChild(form);

    const status = el('p', 'muted');
    status.style.margin = '8px 0 0';
    box.appendChild(status);

    const list = el('div');
    list.style.marginTop = '16px';
    box.appendChild(list);

    async function load() {
      list.replaceChildren();
      try {
        const data = await api('GET', `/api/guilds/${guildId}/stickers`);
        if (!data.stickers.length) {
          list.appendChild(el('p', 'muted', 'No stickers yet.'));
          return;
        }
        const grid = el('div', 'sticker-manage');
        data.stickers.forEach((s) => {
          const cell = el('div', 'sticker-cell');
          const img = el('img');
          img.src = s.url;
          img.alt = s.name;
          cell.appendChild(img);
          cell.appendChild(el('span', 'sticker-name', s.name));
          const rm = el('button', 'icon-btn', '×');
          rm.title = 'Remove sticker';
          rm.onclick = async () => {
            try { await api('DELETE', `/api/stickers/${s.id}`); load(); }
            catch (err) { toast(err.message, 'error'); }
          };
          cell.appendChild(rm);
          grid.appendChild(cell);
        });
        list.appendChild(grid);
      } catch (err) {
        list.appendChild(el('p', 'muted', err.message));
      }
    }

    file.onchange = async () => {
      const chosen = file.files[0];
      file.value = '';
      if (!chosen) return;
      const name = nameInput.value.trim();
      if (!name) { status.textContent = 'Give the sticker a name first.'; return; }
      if (chosen.size > 1024 * 1024) {
        status.textContent = `That image is ${formatBytes(chosen.size)} — the limit is 1 MB.`;
        return;
      }
      status.textContent = 'Uploading…';
      try {
        const res = await fetch(
          `/api/guilds/${guildId}/stickers?name=${encodeURIComponent(name)}`,
          { method: 'POST', body: chosen, credentials: 'same-origin',
            headers: { 'Content-Type': chosen.type } },
        );
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `Upload failed (${res.status})`);
        nameInput.value = '';
        status.textContent = '';
        toast('Sticker added.', 'ok');
        load();
      } catch (err) {
        status.textContent = err.message;
      }
    };

    load();
  });
}

function promptCreateChannel(guildId) {
  openModal((box, close) => {
    box.appendChild(el('h2', null, 'Create a channel'));
    box.appendChild(el('p', 'sub', 'Channel names are lowercase, with dashes for spaces.'));
    const form = el('form');
    const label = el('label', 'field');
    label.appendChild(el('span', null, 'Channel name'));
    const field = el('input');
    field.maxLength = 40;
    field.required = true;
    field.placeholder = 'plans';
    label.appendChild(field);
    form.appendChild(label);
    const actions = el('div', 'modal-actions');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.type = 'button';
    cancel.onclick = close;
    const go = el('button', 'btn primary', 'Create');
    actions.append(cancel, go);
    form.appendChild(actions);
    form.onsubmit = async (e) => {
      e.preventDefault();
      try {
        const data = await api('POST', `/api/guilds/${guildId}/channels`,
          { name: field.value.trim() });
        await refreshSidebarData();
        close();
        openChannel(data.channelId);
      } catch (err) { toast(err.message, 'error'); }
    };
    box.appendChild(form);
  });
}

// ------------------------------------------------------------------ invites

$('#invite-btn').onclick = () => {
  if (state.activeGuildId) showInvites(state.activeGuildId);
  else toast('Open one of your servers first — invites belong to a server.', 'error');
};

function showInvites(guildId) {
  openModal(async (box) => {
    const guild = state.guilds.find((g) => g.id === guildId);
    box.appendChild(el('h2', null, 'Invite people'));
    box.appendChild(el('p', 'sub',
      `Share a code and anyone with an account can join ${guild ? guild.name : 'this server'}.`));

    const controls = el('div');
    controls.style.cssText = 'display:flex;gap:8px;margin-bottom:8px';

    const expiry = el('select');
    expiry.style.cssText =
      'flex:1;padding:10px;border-radius:6px;background:var(--bg-0);border:1px solid transparent';
    [['', 'Never expires'], ['3600', 'Expires in 1 hour'],
     ['86400', 'Expires in 1 day'], ['604800', 'Expires in 7 days']]
      .forEach(([v, t]) => {
        const o = el('option', null, t); o.value = v; expiry.appendChild(o);
      });

    const uses = el('select');
    uses.style.cssText = expiry.style.cssText;
    [['', 'Unlimited uses'], ['1', '1 use'], ['5', '5 uses'], ['25', '25 uses']]
      .forEach(([v, t]) => {
        const o = el('option', null, t); o.value = v; uses.appendChild(o);
      });

    controls.append(expiry, uses);
    box.appendChild(controls);

    const gen = el('button', 'btn primary block', 'Generate invite code');
    box.appendChild(gen);

    const latest = el('div');
    box.appendChild(latest);

    const listHead = el('h3', null, 'Active invites');
    listHead.style.cssText =
      'font-size:12px;text-transform:uppercase;letter-spacing:.5px;' +
      'color:var(--text-mute);margin:22px 0 4px';
    box.appendChild(listHead);
    const list = el('div');
    box.appendChild(list);

    async function loadInvites() {
      list.replaceChildren();
      try {
        const data = await api('GET', `/api/guilds/${guildId}/invites`);
        if (!data.invites.length) {
          list.appendChild(el('p', 'muted', 'No invites yet.'));
          return;
        }
        data.invites.forEach((inv) => {
          const row = el('div', 'invite-row');
          row.appendChild(el('code', null, inv.code));
          const bits = [];
          bits.push(inv.maxUses ? `${inv.uses}/${inv.maxUses} uses` : `${inv.uses} uses`);
          if (inv.expired) bits.push('expired');
          else if (inv.expiresAt) {
            bits.push(`expires ${new Date(inv.expiresAt * 1000).toLocaleString()}`);
          }
          row.appendChild(el('span', 'meta', bits.join(' · ')));
          const copy = el('button', 'btn small ghost', 'Code');
          copy.onclick = () => copyText(inv.code);
          row.appendChild(copy);
          const copyLink = el('button', 'btn small ghost', 'Link');
          copyLink.title = 'Copy a shareable invite link';
          copyLink.onclick = async () => {
            const base = await shareBase();
            copyText(`${base.url}/invite/${inv.code}`);
          };
          row.appendChild(copyLink);
          if (guild && guild.isOwner) {
            const revoke = el('button', 'icon-btn', '×');
            revoke.title = 'Revoke';
            revoke.onclick = async () => {
              try { await api('DELETE', `/api/invites/${inv.code}`); loadInvites(); }
              catch (err) { toast(err.message, 'error'); }
            };
            row.appendChild(revoke);
          }
          list.appendChild(row);
        });
      } catch (err) {
        list.appendChild(el('p', 'muted', err.message));
      }
    }

    gen.onclick = async () => {
      gen.disabled = true;
      try {
        const data = await api('POST', `/api/guilds/${guildId}/invites`, {
          expiresIn: expiry.value ? Number(expiry.value) : null,
          maxUses: uses.value ? Number(uses.value) : null,
        });
        latest.replaceChildren();

        const codeBox = el('div', 'code-box');
        codeBox.appendChild(el('code', null, data.code));
        const copy = el('button', 'btn small', 'Copy code');
        copy.onclick = () => copyText(data.code);
        codeBox.appendChild(copy);
        latest.appendChild(codeBox);

        // "localhost" only means anything on this machine, so offer the address
        // that other people can actually reach.
        const base = await shareBase();
        const link = `${base.url}/invite/${data.code}`;
        const linkBox = el('div', 'code-box');
        const linkText = el('code', 'link-text', link);
        linkBox.appendChild(linkText);
        const copyLink = el('button', 'btn small ghost', 'Copy link');
        copyLink.onclick = () => copyText(link);
        linkBox.appendChild(copyLink);
        latest.appendChild(linkBox);

        latest.appendChild(el('p', 'muted share-hint', base.shareable
          ? 'Send that link to anyone on your Wi-Fi. They can create an account and join.'
          : 'Heads up: this server is only reachable from this computer. Restart it '
            + 'without --host 127.0.0.1 so other people can open the link.'));

        loadInvites();
      } catch (err) { toast(err.message, 'error'); }
      finally { gen.disabled = false; }
    };

    loadInvites();
  });
}

/** Base URL to hand out in invite links, cached for the session. */
let shareBaseCache = null;

async function shareBase() {
  if (shareBaseCache) return shareBaseCache;
  // If the owner already browsed here by IP or hostname, that address demonstrably
  // works — keep it. Only "localhost" needs replacing.
  const onLocalhost = /^(localhost|127\.|\[?::1)/.test(location.hostname);
  if (!onLocalhost) {
    shareBaseCache = { url: location.origin, shareable: true };
    return shareBaseCache;
  }
  try {
    const info = await api('GET', '/api/server-info');
    shareBaseCache = { url: info.lanUrl, shareable: info.isShareable };
  } catch {
    shareBaseCache = { url: location.origin, shareable: false };
  }
  return shareBaseCache;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('Copied to clipboard.', 'ok');
  } catch {
    const ta = el('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); toast('Copied to clipboard.', 'ok'); }
    catch { toast(text); }
    ta.remove();
  }
}

// ------------------------------------------------------------------ profiles

function showBotPanel() {
  openModal((box, close) => {
    const head = el('div', 'profile-head');
    // Pull the real member-list entry so the mask shows, rather than an initial.
    const bot = state.members.find((m) => m.isBot);
    head.appendChild(avatar(bot || { username: 'Frontman', color: '#1f2126' }, 'xl'));
    const meta = el('div');
    meta.appendChild(el('h2', null, 'Frontman'));
    meta.appendChild(el('p', 'muted', "Runs the games in every server."));
    head.appendChild(meta);
    box.appendChild(head);

    box.appendChild(el('p', 'sub',
      'Start a hand from the message box with a slash command, or use a button below.'));

    const stack = el('div');
    stack.style.cssText = 'display:flex;flex-direction:column;gap:8px';
    availableCommands().forEach((c) => {
      const b = el('button', 'btn');
      b.appendChild(el('strong', null, `${c.name} ${c.args}`));
      b.appendChild(el('span', 'muted', ` — ${c.summary}`));
      b.onclick = () => {
        close();
        if (!state.activeChannelId) {
          toast('Open a channel first.', 'error');
          return;
        }
        c.run();
      };
      stack.appendChild(b);
    });
    box.appendChild(stack);
  });
}

async function showProfile(userId) {
  let user;
  try { user = (await api('GET', `/api/users/${userId}`)).user; }
  catch (err) { return toast(err.message, 'error'); }

  openModal((box, close) => {
    const head = el('div', 'profile-head');
    head.appendChild(avatar(user, 'xl'));
    const meta = el('div');
    meta.appendChild(el('h2', null, user.username));
    meta.appendChild(el('p', 'muted', user.tag));
    if (user.status) meta.appendChild(el('p', 'profile-status', user.status));
    meta.appendChild(el('p', 'muted', user.online ? 'Online now' : 'Offline'));
    head.appendChild(meta);
    box.appendChild(head);

    if (user.bio) {
      const bio = el('p', null, user.bio);
      bio.style.marginBottom = '16px';
      box.appendChild(bio);
    }

    if (user.id === state.me.id) {
      const note = el('p', 'muted', 'This is you.');
      box.appendChild(note);
      return;
    }

    const stack = el('div');
    stack.style.cssText = 'display:flex;flex-direction:column;gap:8px';

    const msg = el('button', 'btn primary', 'Send message');
    msg.onclick = async () => {
      try {
        const d = await api('POST', '/api/dms', { userId: user.id });
        await refreshSidebarData();
        close();
        openChannel(d.channelId);
      } catch (err) { toast(err.message, 'error'); }
    };
    stack.appendChild(msg);

    const f = user.friendship;
    if (!f) {
      const add = el('button', 'btn', 'Add friend');
      add.onclick = async () => {
        try {
          await api('POST', '/api/friends', { tag: user.tag });
          toast('Friend request sent.', 'ok');
          await refreshAll();
          close();
        } catch (err) { toast(err.message, 'error'); }
      };
      stack.appendChild(add);
    } else if (f.status === 'pending' && f.incoming) {
      const accept = el('button', 'btn', 'Accept friend request');
      accept.onclick = async () => {
        await api('POST', `/api/friends/${f.id}/accept`);
        await refreshAll();
        close();
      };
      stack.appendChild(accept);
    } else if (f.status === 'pending') {
      stack.appendChild(el('p', 'muted', 'Friend request pending.'));
    } else {
      const rm = el('button', 'btn danger', 'Remove friend');
      rm.onclick = async () => {
        await api('DELETE', `/api/friends/${f.id}`);
        await refreshAll();
        close();
      };
      stack.appendChild(rm);
    }

    const guild = state.guilds.find((g) => g.id === state.activeGuildId);
    if (guild && guild.isOwner && user.id !== state.me.id) {
      const inGuild = state.members.some((m) => m.id === user.id);
      if (inGuild) {
        const kick = el('button', 'btn danger', `Remove from ${guild.name}`);
        kick.onclick = () => {
          close();
          confirmModal('Remove member?',
            `${user.tag} will lose access to ${guild.name} until they are invited again.`,
            'Remove', async () => {
              await api('DELETE', `/api/guilds/${guild.id}/members/${user.id}`);
              renderMembers();
            });
        };
        stack.appendChild(kick);
      }
    }
    box.appendChild(stack);
  });
}

// ------------------------------------------------------------------ settings

$('#me-settings').onclick = () => {
  openModal((box, close) => {
    box.appendChild(el('h2', null, 'Your profile'));
    box.appendChild(el('p', 'sub', `Signed in as ${state.me.email}`));

    // ---- profile picture
    const picRow = el('div', 'pic-row');
    let picPreview = avatar(state.me, 'xl');
    picRow.appendChild(picPreview);

    const picButtons = el('div', 'pic-buttons');
    const picFile = el('input');
    picFile.type = 'file';
    picFile.accept = 'image/png,image/jpeg,image/gif,image/webp';
    picFile.hidden = true;
    const upload = el('button', 'btn small', 'Upload picture');
    upload.type = 'button';
    upload.onclick = () => picFile.click();
    picButtons.append(upload, picFile);

    const removePic = el('button', 'btn small ghost', 'Remove');
    removePic.type = 'button';
    removePic.hidden = !state.me.avatarUrl;
    picButtons.appendChild(removePic);

    const picStatus = el('small', 'muted');
    picButtons.appendChild(picStatus);
    picRow.appendChild(picButtons);
    box.appendChild(picRow);

    const refreshPic = () => {
      const fresh = avatar(state.me, 'xl');
      picPreview.replaceWith(fresh);
      picPreview = fresh;
      removePic.hidden = !state.me.avatarUrl;
    };

    picFile.onchange = async () => {
      const chosen = picFile.files[0];
      picFile.value = '';
      if (!chosen) return;
      if (chosen.size > 2 * 1024 * 1024) {
        picStatus.textContent = `That image is ${formatBytes(chosen.size)} — the limit is 2 MB.`;
        return;
      }
      picStatus.textContent = 'Uploading…';
      try {
        const res = await fetch('/api/me/avatar', {
          method: 'POST', body: chosen, credentials: 'same-origin',
          headers: { 'Content-Type': chosen.type },
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `Upload failed (${res.status})`);
        state.me = data.user;
        picStatus.textContent = '';
        refreshPic();
        renderMe();
        renderMessages();
        toast('Profile picture updated.', 'ok');
      } catch (err) {
        picStatus.textContent = err.message;
      }
    };

    removePic.onclick = async () => {
      try {
        const data = await api('DELETE', '/api/me/avatar');
        state.me = data.user;
        refreshPic();
        renderMe();
        renderMessages();
      } catch (err) { toast(err.message, 'error'); }
    };

    const form = el('form');

    const nameLabel = el('label', 'field');
    nameLabel.appendChild(el('span', null, 'Username'));
    const nameInput = el('input');
    nameInput.value = state.me.username;
    nameInput.maxLength = 32;
    nameLabel.appendChild(nameInput);
    nameLabel.appendChild(el('small', null,
      `Changing your username assigns a new tag. Yours is currently ${state.me.tag}.`));
    form.appendChild(nameLabel);

    const statusLabel = el('label', 'field');
    statusLabel.appendChild(el('span', null, 'Status'));
    const statusInput = el('input');
    statusInput.value = state.me.status || '';
    statusInput.maxLength = 60;
    statusInput.placeholder = 'Working on the site';
    statusLabel.appendChild(statusInput);
    statusLabel.appendChild(el('small', null,
      'One short line, shown under your name in the member list.'));
    form.appendChild(statusLabel);

    const bioLabel = el('label', 'field');
    bioLabel.appendChild(el('span', null, 'About me'));
    const bioInput = el('textarea');
    bioInput.rows = 3;
    bioInput.maxLength = 190;
    bioInput.value = state.me.bio || '';
    bioLabel.appendChild(bioInput);
    form.appendChild(bioLabel);

    const soundLabel = el('label', 'field toggle-field');
    const soundBox = el('input');
    soundBox.type = 'checkbox';
    soundBox.checked = pingSoundEnabled();
    soundBox.onchange = () => {
      setPingSound(soundBox.checked);
      if (soundBox.checked) playPing();          // let them hear it
    };
    soundLabel.appendChild(soundBox);
    const soundText = el('div');
    soundText.appendChild(el('strong', null, 'Play a sound when I\'m mentioned'));
    soundText.appendChild(el('small', null,
      'Chimes for @mentions and @everyone, in any channel.'));
    soundLabel.appendChild(soundText);
    form.appendChild(soundLabel);

    // Only shown to people who bought one — there is nothing to toggle
    // otherwise, and an empty switch just raises questions.
    if (hasPerk(state.me, 'fedora')) {
      const hatLabel = el('label', 'field toggle-field');
      const hatBox = el('input');
      hatBox.type = 'checkbox';
      hatBox.checked = state.me.decoration === 'fedora';
      hatBox.onchange = async () => {
        try {
          const data = await api('PATCH', '/api/me',
            { decoration: hatBox.checked ? 'fedora' : '' });
          state.me = data.user;
          refreshPic();
          renderMe();
          renderMessages();
          renderMembers();
        } catch (err) {
          toast(err.message, 'error');
          hatBox.checked = !hatBox.checked;
        }
      };
      hatLabel.appendChild(hatBox);
      const hatText = el('div');
      hatText.appendChild(el('strong', null, 'Wear the fedora 🎩'));
      hatText.appendChild(el('small', null,
        'Sits on your avatar wherever it appears. Take it off and back on as '
        + 'often as you like — it stays yours.'));
      hatLabel.appendChild(hatText);
      form.appendChild(hatLabel);
    }

    const colorLabel = el('label', 'field');
    colorLabel.appendChild(el('span', null, 'Avatar colour'));
    const colorInput = el('input');
    colorInput.type = 'color';
    colorInput.value = state.me.color;
    colorInput.style.height = '42px';
    colorLabel.appendChild(colorInput);
    form.appendChild(colorLabel);

    const actions = el('div', 'modal-actions');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.type = 'button';
    cancel.onclick = close;
    const save = el('button', 'btn primary', 'Save changes');
    actions.append(cancel, save);
    form.appendChild(actions);

    form.onsubmit = async (e) => {
      e.preventDefault();
      save.disabled = true;
      try {
        const data = await api('PATCH', '/api/me', {
          username: nameInput.value.trim(),
          status: statusInput.value,
          bio: bioInput.value,
          color: colorInput.value,
        });
        state.me = data.user;
        renderMe();
        await refreshAll();
        if (state.activeChannelId) renderMessages();
        close();
        toast('Profile updated.', 'ok');
      } catch (err) { toast(err.message, 'error'); save.disabled = false; }
    };
    box.appendChild(form);
  });
};

$('#me-logout').onclick = () => {
  confirmModal('Sign out?', 'You can sign back in any time.', 'Sign out', async () => {
    stopPoll();
    await api('POST', '/api/logout');
    state.me = null;
    showAuth();
  });
};

function renderMe() {
  const fresh = avatar({ ...state.me, online: true });
  fresh.id = 'me-avatar';
  $('#me-avatar').replaceWith(fresh);
  $('#me-name').textContent = state.me.username;
  // The status replaces the tag when set — there's only room for one line.
  const sub = $('#me-tag');
  sub.textContent = state.me.status || `#${state.me.discriminator}`;
  sub.title = state.me.status ? `${state.me.status} · ${state.me.tag}` : state.me.tag;
}

// ------------------------------------------------------------------ boot

function showAuth() {
  stopPoll();
  stopApprovalWatch();
  $('#app').classList.add('hidden');
  $('#pending').classList.add('hidden');
  $('#auth').classList.remove('hidden');
  $('#boot').classList.add('hidden');
  setAuthMode('login');
}

// ------------------------------------------------------ waiting to be let in

let approvalTimer = null;

function stopApprovalWatch() {
  if (approvalTimer) { clearInterval(approvalTimer); approvalTimer = null; }
}

/** The holding screen for an account nobody has approved yet. */
function showPending() {
  stopPoll();
  $('#app').classList.add('hidden');
  $('#auth').classList.add('hidden');
  $('#boot').classList.add('hidden');
  $('#pending').classList.remove('hidden');

  const declined = state.me && state.me.signupStatus === 'declined';
  $('#pending-title').textContent = declined
    ? 'We couldn’t let you in' : 'Thanks for joining';
  $('#pending-text').textContent = declined
    ? 'An administrator has reviewed your application and turned it down. '
      + 'If you think that’s a mistake, speak to whoever runs this site.'
    : 'Thanks for joining our learning resource manager. We will approve you '
      + 'shortly.';
  $('#pending-wait').hidden = declined;
  $('#pending-note').hidden = declined;

  // Nothing pushes to an account that can't poll, so check back periodically.
  // The moment they're approved they land in the app without touching a thing.
  stopApprovalWatch();
  if (!declined) {
    approvalTimer = setInterval(async () => {
      try {
        const data = await api('GET', '/api/me');
        if (!data.user) return showAuth();
        state.me = data.user;
        if (data.user.approved) {
          stopApprovalWatch();
          $('#pending').classList.add('hidden');
          toast('You’re in. Welcome.', 'ok');
          await enterApp();
        } else if (data.user.signupStatus === 'declined') {
          showPending();
        }
      } catch { /* try again on the next tick */ }
    }, 5000);
  }
}

$('#pending-signout').onclick = async () => {
  stopApprovalWatch();
  try { await api('POST', '/api/logout'); } catch { /* going anyway */ }
  state.me = null;
  showAuth();
};

async function enterApp() {
  $('#auth').classList.add('hidden');
  $('#app').classList.remove('hidden');
  $('#boot').classList.add('hidden');
  renderMe();
  await refreshSidebarData();
  renderRail();

  const inviteMatch = location.pathname.match(/^\/invite\/([A-Za-z0-9]+)$/);
  if (inviteMatch) {
    openFriends();
    handleInviteLink(inviteMatch[1]);
    return;
  }

  if (state.dms.length) await openChannel(state.dms[0].channelId);
  else if (state.guilds.length) await openGuild(state.guilds[0].id);
  else openFriends();
}

async function handleInviteLink(code) {
  let preview;
  try { preview = await api('GET', `/api/invites/${code}`); }
  catch (err) {
    toast(err.message, 'error');
    history.replaceState({}, '', '/');
    return;
  }
  openModal((box, close) => {
    box.appendChild(el('h2', null,
      preview.alreadyMember ? 'You are already in this server' : 'You have been invited'));
    const head = el('div', 'profile-head');
    const icon = el('div', 'avatar xl',
      preview.guild.name.split(/\s+/).slice(0, 2).map((w) => w[0]).join('').toUpperCase());
    icon.style.background = preview.guild.color;
    icon.style.borderRadius = '18px';
    head.appendChild(icon);
    const meta = el('div');
    meta.appendChild(el('h2', null, preview.guild.name));
    meta.appendChild(el('p', 'muted',
      `${preview.guild.memberCount} member${preview.guild.memberCount === 1 ? '' : 's'}`));
    head.appendChild(meta);
    box.appendChild(head);

    const go = el('button', 'btn primary block',
      preview.alreadyMember ? 'Open server' : 'Accept invite');
    go.onclick = async () => {
      go.disabled = true;
      try {
        const data = await api('POST', `/api/invites/${code}/join`);
        await refreshSidebarData();
        close();
        history.replaceState({}, '', '/');
        if (data.channelId) {
          state.activeGuildId = data.guildId;
          await openChannel(data.channelId);
        }
      } catch (err) { toast(err.message, 'error'); go.disabled = false; }
    };
    box.appendChild(go);
  });
}

(async function boot() {
  let data;
  try {
    data = await api('GET', '/api/me');
  } catch {
    showAuth();                       // can't reach the server at all
    return;
  }

  if (!data.user) {
    showAuth();
    const m = location.pathname.match(/^\/invite\/([A-Za-z0-9]+)$/);
    if (m) toast('Sign in or create an account to accept the invite.');
    return;
  }

  state.me = data.user;
  if (!data.user.approved) {
    showPending();
    return;
  }
  try {
    await enterApp();
  } catch (err) {
    // We are signed in — a failure loading servers or messages must not dump
    // the user back to the login screen as if the session had ended.
    $('#auth').classList.add('hidden');
    $('#app').classList.remove('hidden');
    $('#boot').classList.add('hidden');
    renderMe();
    toast(err.message || 'Some things failed to load.', 'error');
    openFriends();
  }
})();
