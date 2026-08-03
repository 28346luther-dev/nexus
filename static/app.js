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
  return node;
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
  $('#auth-form').username.required = registering;
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
    if (authMode === 'register') payload.username = form.username.value;
    const data = await api('POST', `/api/${authMode}`, payload);
    state.me = data.user;
    form.reset();
    await enterApp();
  } catch (err) {
    $('#auth-error').textContent = err.message;
  } finally {
    btn.disabled = false;
  }
};

// ------------------------------------------------------------------ render: rail

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
    btn.style.background = state.activeGuildId === g.id ? g.color : 'var(--bg-3)';
    btn.textContent = g.name.split(/\s+/).slice(0, 2).map((w) => w[0]).join('').toUpperCase();
    btn.classList.toggle('active', state.activeGuildId === g.id);
    const unread = g.channels.reduce((n, c) => n + c.unread, 0);
    const pings = g.channels.reduce((n, c) => n + (c.mentions || 0), 0);
    if (pings > 0) {
      const b = el('span', 'rail-badge ping', pings > 99 ? '99+' : `@${pings}`);
      b.title = `${pings} message${pings === 1 ? '' : 's'} mentioning you`;
      btn.appendChild(b);
      if (state.activeGuildId !== g.id) btn.classList.add('pinged');
    } else if (unread > 0) {
      btn.appendChild(el('span', 'rail-badge', unread > 99 ? '99+' : String(unread)));
      if (state.activeGuildId !== g.id) btn.classList.add('pinged');
    }
    btn.onmouseenter = () => { btn.style.background = g.color; };
    btn.onmouseleave = () => {
      if (state.activeGuildId !== g.id) btn.style.background = 'var(--bg-3)';
    };
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
      item.classList.toggle('active', c.id === state.activeChannelId);
      if (c.unread > 0) item.classList.add('unread');
      item.appendChild(el('span', 'hash', '#'));
      item.appendChild(el('span', 'label', c.name));
      if (c.mentions > 0 && c.id !== state.activeChannelId) {
        const ping = el('span', 'badge ping', c.mentions > 99 ? '99+' : `@${c.mentions}`);
        ping.title = `${c.mentions} message${c.mentions === 1 ? '' : 's'} mentioning you`;
        item.appendChild(ping);
      } else if (c.unread > 0 && c.id !== state.activeChannelId) {
        item.appendChild(el('span', 'badge', c.unread > 99 ? '99+' : String(c.unread)));
      }
      item.onclick = () => openChannel(c.id);
      if (guild.isOwner && guild.channels.length > 1) {
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
    line.appendChild(el('span', 'm-name', m.username));
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
  // Replies and game cards always show their own header.
  if (m.replyToId || m.game) grouped = false;
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
    const author = el('span', 'msg-author', m.author.username);
    author.style.color = m.author.color;
    author.onclick = () => showProfile(m.author.id);
    head.appendChild(author);
    if (m.author.isBot) head.appendChild(el('span', 'bot-tag', 'BOT'));
    head.appendChild(el('span', 'msg-time', stampLabel(m.createdAt)));
    body.appendChild(head);
  }
  const text = el('div', 'msg-text');
  text.innerHTML = formatContent(m.content);
  if (m.editedAt) {
    const tag = el('span', 'edited', '(edited)');
    tag.title = stampLabel(m.editedAt);
    text.appendChild(tag);
  }
  if (m.content) body.appendChild(text);

  if (m.game) body.appendChild(gameNode(m));
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

  const head = el('div', 'seat-head');
  if (seat.user) head.appendChild(avatar({ ...seat.user, online: undefined }, 'xs'));
  head.appendChild(el('span', 'seat-name', seat.user ? seat.user.username : label));
  if (seat.total !== null) {
    const total = el('span', 'seat-total', String(seat.total));
    if (seat.busted) total.classList.add('bust');
    head.appendChild(total);
  } else {
    head.appendChild(el('span', 'seat-total hidden-total', '?'));
  }
  if (seat.busted) head.appendChild(el('span', 'seat-flag bust-flag', 'BUST'));
  else if (seat.stood) head.appendChild(el('span', 'seat-flag', 'STAND'));
  wrap.appendChild(head);

  const hand = el('div', 'hand');
  seat.cards.forEach((c) => hand.appendChild(cardNode(c)));
  wrap.appendChild(hand);
  return wrap;
}

function slotsNode(g) {
  const card = el('div', `game-card slots ${g.profit > 0 ? 'won' : ''}`.trim());
  const head = el('div', 'game-head');
  head.appendChild(el('span', 'game-title', 'Slot machine'));
  head.appendChild(el('span', 'game-status', g.bet ? coins(g.bet) : 'for fun'));
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
    again.onclick = () => spinSlots(g.bet || 10);
    actions.appendChild(again);
    card.appendChild(actions);
  }

  const foot = el('div', 'game-foot');
  foot.textContent = `${g.player ? g.player.username : 'someone'}'s spin`;
  card.appendChild(foot);
  return card;
}

function gameNode(m) {
  const g = m.game;
  if (g.mode === 'slots') return slotsNode(g);

  const card = el('div', `game-card ${g.status}`.trim());

  const head = el('div', 'game-head');
  head.appendChild(el('span', 'game-title',
    g.mode === 'cpu' ? 'Blackjack' : 'Blackjack · 1v1'));
  if (g.bet) {
    const stake = el('span', 'game-stake', coins(g.bet));
    stake.title = g.mode === 'pvp' ? `Pot: ${coins(g.bet * 2)}` : 'Your stake';
    head.appendChild(stake);
  }
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
    ['hit', 'stand'].forEach((action) => {
      const b = el('button', `btn small ${action === 'hit' ? 'primary' : ''}`.trim(),
        action === 'hit' ? 'Hit' : 'Stand');
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
    again.onclick = () => startGame(g.mode, g.bet);
    actions.appendChild(again);
  }
  if (actions.children.length) card.appendChild(actions);

  const foot = el('div', 'game-foot');
  const hostName = g.seats.host.user ? g.seats.host.user.username : 'someone';
  foot.textContent = `${hostName}'s game`;
  card.appendChild(foot);
  return card;
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
    pill.onmouseenter = async () => {
      if (pill.dataset.loaded) return;
      pill.dataset.loaded = '1';
      try {
        const data = await api('GET', `/api/messages/${m.id}/reactions`);
        const names = data.reactors[r.emoji] || [];
        pill.title = `${names.join(', ')} reacted with ${r.emoji}`;
      } catch { /* tooltip is optional */ }
    };
    bar.appendChild(pill);
  });

  const add = el('button', 'pill add-pill', '＋');
  add.title = 'Add reaction';
  add.onclick = (e) => openEmojiPicker(e.currentTarget, (emoji) => react_(m.id, emoji));
  bar.appendChild(add);
  return bar;
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

function coins(n) {
  return `${Number(n).toLocaleString()} ${COIN}`;
}

const COMMANDS = [
  {
    name: '/blackjack',
    args: 'cpu',
    summary: 'Play a hand against the Gamesman',
    takesNumber: true,
    optionalNumber: true,
    run: (bet) => askStake('Blackjack vs the Gamesman', bet, (n) => startGame('cpu', n)),
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
    name: '/slots',
    args: '<bet>',
    summary: 'Spin the slot machine',
    takesNumber: true,
    optionalNumber: true,
    run: (bet) => askStake('Slot machine', bet, spinSlots, 10),
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

async function startGame(mode, bet = 0) {
  if (!state.activeChannelId) return;
  try {
    const data = await api('POST', `/api/channels/${state.activeChannelId}/games`,
      { mode, bet });
    if (data.wallet) state.wallet = data.wallet;
    pushMessage(data.message);
  } catch (err) { toast(err.message, 'error'); }
}

async function spinSlots(bet) {
  if (!state.activeChannelId) return;
  try {
    const data = await api('POST', `/api/channels/${state.activeChannelId}/slots`,
      { bet });
    if (data.wallet) state.wallet = data.wallet;
    pushMessage(data.message);
  } catch (err) { toast(err.message, 'error'); }
}

async function claimDaily() {
  try {
    const data = await api('POST', '/api/wallet/claim');
    state.wallet = data;
    toast(`Claimed ${coins(data.claimed)} — you now have ${coins(data.coins)}.`, 'ok');
  } catch (err) { toast(err.message, 'error'); }
}

function statLine(label, value) {
  const row = el('div', 'stat');
  row.appendChild(el('span', 'stat-label', label));
  row.appendChild(el('span', 'stat-value', value));
  return row;
}

async function showWallet() {
  const w = await refreshWallet();
  if (!w) return;
  openModal((box) => {
    box.appendChild(el('h2', null, 'Your wallet'));
    box.appendChild(el('p', 'sub', `${w.stats.rank} · ${coins(w.coins)}`));

    const grid = el('div', 'stat-grid');
    grid.appendChild(statLine('Won', String(w.stats.wins)));
    grid.appendChild(statLine('Lost', String(w.stats.losses)));
    grid.appendChild(statLine('Pushed', String(w.stats.pushes)));
    grid.appendChild(statLine('Net', `${w.stats.net >= 0 ? '+' : ''}${w.stats.net.toLocaleString()}`));
    grid.appendChild(statLine('Best win', w.stats.biggestWin.toLocaleString()));
    grid.appendChild(statLine('Best streak', String(w.stats.bestStreak)));
    box.appendChild(grid);

    if (w.stats.nextRank) {
      box.appendChild(el('p', 'muted',
        `${w.stats.nextRank.winsAway} more win${w.stats.nextRank.winsAway === 1 ? '' : 's'}`
        + ` to reach ${w.stats.nextRank.name}.`));
    } else {
      box.appendChild(el('p', 'muted', 'Top rank reached.'));
    }

    const claim = el('button', 'btn primary block', w.canClaim
      ? `Claim ${coins(w.dailyAmount)}`
      : `Next claim in ${formatWait(w.claimIn)}`);
    claim.disabled = !w.canClaim;
    claim.style.marginTop = '16px';
    claim.onclick = async () => { closeModal(); await claimDaily(); };
    box.appendChild(claim);
  });
}

function formatWait(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

async function showBank() {
  if (!state.activeGuildId) {
    toast('Open a server to see its bank.', 'error');
    return;
  }
  let data;
  try { data = await api('GET', `/api/guilds/${state.activeGuildId}/bank`); }
  catch (err) { return toast(err.message, 'error'); }

  openModal((box) => {
    box.appendChild(el('h2', null, 'Sana Coin bank'));
    box.appendChild(el('p', 'sub',
      `${coins(data.total)} in circulation across ${data.accounts.length} `
      + `account${data.accounts.length === 1 ? '' : 's'}.`));
    const list = el('div', 'rank-list');
    data.accounts.forEach((a, i) => {
      const row = el('div', 'rank-row');
      row.appendChild(el('span', 'rank-pos', String(i + 1)));
      row.appendChild(avatar(a));
      const who = el('div', 'rank-who');
      who.appendChild(el('strong', null, a.username));
      who.appendChild(el('small', null, a.rank));
      row.appendChild(who);
      row.appendChild(el('span', 'rank-score', coins(a.coins)));
      list.appendChild(row);
    });
    box.appendChild(list);
  });
}

async function showLeaderboard() {
  if (!state.activeGuildId) {
    toast('Open a server to see its leaderboard.', 'error');
    return;
  }
  let data;
  try { data = await api('GET', `/api/guilds/${state.activeGuildId}/leaderboard`); }
  catch (err) { return toast(err.message, 'error'); }

  openModal((box) => {
    box.appendChild(el('h2', null, 'Leaderboard'));
    box.appendChild(el('p', 'sub', 'Ranked by games won.'));
    if (!data.players.length) {
      box.appendChild(el('p', 'muted', 'Nobody has played yet.'));
      return;
    }
    const list = el('div', 'rank-list');
    data.players.forEach((p, i) => {
      const row = el('div', 'rank-row');
      const pos = el('span', 'rank-pos', String(i + 1));
      if (i < 3) pos.classList.add('podium');
      row.appendChild(pos);
      row.appendChild(avatar(p));
      const who = el('div', 'rank-who');
      who.appendChild(el('strong', null, p.username));
      who.appendChild(el('small', null,
        `${p.rank} · ${p.wins}W / ${p.losses}L · best streak ${p.bestStreak}`));
      row.appendChild(who);
      const net = el('span', `rank-score ${p.net >= 0 ? 'up' : 'down'}`,
        `${p.net >= 0 ? '+' : ''}${p.net.toLocaleString()}`);
      row.appendChild(net);
      list.appendChild(row);
    });
    box.appendChild(list);
  });
}

/** Match typed text against a command; null when it isn't one. */
function matchCommand(text) {
  const trimmed = text.trim();
  if (!trimmed.startsWith('/')) return null;
  const [verbRaw, ...rest] = trimmed.split(/\s+/);
  const verb = verbRaw.toLowerCase();
  const tail = rest.join(' ').toLowerCase();

  const numeric = COMMANDS.find((c) => c.name === verb && c.takesNumber);
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
          // Chime once per batch, not once per message.
          if (fresh.some((m) => m.mentionsMe && m.author.id !== state.me.id)) playPing();
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

/** Unread pings across every server channel and DM. */
function totalMentions() {
  const inGuilds = state.guilds.reduce(
    (n, g) => n + g.channels.reduce((c, ch) => c + (ch.mentions || 0), 0), 0);
  const inDms = state.dms.reduce((n, d) => n + (d.mentions || 0), 0);
  return inGuilds + inDms;
}

async function refreshSidebarData() {
  const [guilds, dms, friends, wallet] = await Promise.all([
    api('GET', '/api/guilds'),
    api('GET', '/api/dms'),
    api('GET', '/api/friends'),
    api('GET', '/api/wallet').catch(() => null),
  ]);
  if (wallet) state.wallet = wallet;
  state.guilds = guilds.guilds;
  state.dms = dms.dms;
  state.friends = friends;
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

      const stick = el('button', 'btn', 'Manage stickers');
      stick.onclick = () => { close(); manageStickers(guild.id); };
      stack.appendChild(stick);

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
    const icon = el('div', 'avatar xl', 'G');
    icon.style.background = '#3ba55d';
    head.appendChild(icon);
    const meta = el('div');
    meta.appendChild(el('h2', null, 'Gamesman'));
    meta.appendChild(el('p', 'muted', 'Deals blackjack in every server.'));
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
  $('#app').classList.add('hidden');
  $('#auth').classList.remove('hidden');
  $('#boot').classList.add('hidden');
  setAuthMode('login');
}

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
