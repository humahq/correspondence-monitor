/* Admin pages: a small React app (React 18 + htm, no build step). Data comes from /admin/api/...; links open
   in place with history.pushState, and the alert and answer pages (/items, /answers) open in a new tab. */
(function () {
  "use strict";
  const { useState, useEffect } = React;
  const html = htm.bind(React.createElement);
  const EMAIL = document.body.dataset.email || "";
  const BOT_NAME = document.body.dataset.botName || "Study monitor";
  const BOT_AVATAR = document.body.dataset.botAvatar || "";

  const NAV = [["/admin", "Home"], ["/admin/messages", "Messages"], ["/admin/alerts", "Alerts"],
               ["/admin/memory", "Patient memory"], ["/admin/people", "Sign-ins and use"]];

  function go(to) { history.pushState(null, "", to); window.dispatchEvent(new Event("popstate")); window.scrollTo(0, 0); }
  function linkTo(to) {
    return (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
      e.preventDefault(); go(to);
    };
  }

  function useLocation() {
    const [loc, setLoc] = useState(location.pathname + location.search);
    useEffect(() => {
      const on = () => setLoc(location.pathname + location.search);
      window.addEventListener("popstate", on);
      return () => window.removeEventListener("popstate", on);
    }, []);
    return loc;
  }

  function useData(url) {
    const [state, setState] = useState({ loading: true });
    useEffect(() => {
      let live = true;
      setState((s) => ({ ...s, loading: true }));
      fetch(url, { credentials: "same-origin", redirect: "manual" })
        .then((r) => {
          if (r.type === "opaqueredirect" || r.status === 302) throw new Error("signin");
          if (r.status === 404) throw new Error("notfound");
          if (!r.ok) throw new Error("http " + r.status);
          return r.json();
        })
        .then((data) => live && setState({ data }))
        .catch((e) => live && setState({ error: e.message }));
      return () => { live = false; };
    }, [url]);
    return state;
  }

  function Loading({ state, children }) {
    if (state.error === "signin")
      return html`<div class="error">Your sign-in has ended. <a href=${location.pathname}>Sign in again</a>.</div>`;
    if (state.error === "notfound") return html`<div class="error">Not found.</div>`;
    if (state.error) return html`<div class="error">Could not load this page (${state.error}). Please reload.</div>`;
    if (!state.data) return html`<div class="muted">Loading…</div>`;
    return children(state.data);
  }

  const Badge = ({ cls, children, dot }) => html`<span class=${"badge " + (cls || "") + (dot ? " dot" : "")}>${children}</span>`;
  const When = ({ w }) => w && w.at ? html`<span title=${w.text}>${w.text}</span>` : html`<span class="muted">never</span>`;

  function Select({ value, onChange, options }) {
    return html`<select value=${value} onChange=${(e) => onChange(e.target.value)}>
      ${options.map((o) => html`<option key=${o.value} value=${o.value}>${o.label}</option>`)}</select>`;
  }

  function useQuery(defaults) {
    const params = new URLSearchParams(location.search);
    const q = {};
    for (const k in defaults) q[k] = params.get(k) ?? defaults[k];
    const set = (k, v) => {
      const next = new URLSearchParams({ ...q, ...(typeof k === "object" ? k : { [k]: v }) });
      next.delete("before");
      for (const [key, val] of [...next]) if (val === "") next.delete(key);
      go(location.pathname + "?" + next.toString());
    };
    return [q, set];
  }

  // Links in a message: the chat form <address|words>, <address>, [words](address) and bare addresses.
  const LINK = /<(https?:\/\/[^\s|<>]+)\|([^<>]+)>|<(https?:\/\/[^\s<>]+)>|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>"]*[^\s<>".,;:!?)\]'])/g;
  function linkify(text) {
    const out = [];
    let last = 0, m;
    LINK.lastIndex = 0;
    while ((m = LINK.exec(text))) {
      if (m.index > last) out.push(text.slice(last, m.index));
      const url = m[1] || m[3] || m[5] || m[6];
      out.push(html`<a key=${m.index} href=${url} target="_blank" rel="noopener noreferrer">${m[2] || m[4] || url}</a>`);
      last = LINK.lastIndex;
    }
    out.push(text.slice(last));
    return out;
  }

  // Which days a page shows. The server works in the study's local days.
  const PERIODS = [{ value: "1", label: "Today" }, { value: "yesterday", label: "Yesterday" },
    { value: "7", label: "Last 7 days" }, { value: "30", label: "Last 30 days" }, { value: "all", label: "All time" }];
  const todayIso = () => new Date().toLocaleDateString("en-CA");
  function Period({ q, set }) {
    const opts = q.date ? PERIODS.concat([{ value: "date", label: "One day" }]) : PERIODS;
    return html`<${Select} value=${q.date ? "date" : q.days} options=${opts}
        onChange=${(v) => v !== "date" && set({ days: v, date: "" })} />
      <label class="pick">or pick a day <input type="date" value=${q.date} max=${todayIso()}
        onChange=${(e) => set({ date: e.target.value })} /></label>`;
  }
  const periodOf = (q) => (q.date ? { date: q.date } : { days: q.days });
  const urlWith = (path, params) => path + "?" + new URLSearchParams(params).toString();
  const Count = ({ n, href, cls }) => n
    ? html`<a class=${"count " + (cls || "")} href=${href} onClick=${linkTo(href)}>${n}</a>`
    : html`<span class="muted">–</span>`;

  function SentTo({ s }) {
    if (!s.people.length && !s.chats.length)
      return html`<span class="muted">${s.not_sent ? s.not_sent + " waiting, not sent" : "nobody"}</span>`;
    return html`<div class="sentto">${s.people.map((p) => html`<div key=${p.email}>
        ${p.how === "copy" && html`<span class="muted">copy: </span>`}${p.name}
        <span class="small muted"> ${p.role}</span></div>`)}
      ${s.chats.length > 0 && html`<div class="small muted">${s.people.length ? "and posted in the " : "posted in the "}${s.chats.join(", ")}</div>`}
      ${s.not_sent > 0 && html`<div class="small muted">${s.not_sent} more waiting, not sent</div>`}</div>`;
  }

  function AlertTags({ a }) {
    return html`<${Badge} cls=${a.type.kind}>${a.type.label}<//> <${Badge} cls=${a.tier}>${a.tier_label}<//>`;
  }

  function Outcome({ items }) {
    return html`<div class="outcome">${items.map((o, i) => o.alert
      ? html`<span key=${i}><${AlertTags} a=${o.alert} /> <a href=${o.alert.link} target="_blank" rel="noopener"
          onClick=${(e) => e.stopPropagation()}>${o.text}</a></span>`
      : html`<span key=${i} class=${o.kind}>${o.kind === "error" || o.kind === "waiting"
          ? html`<${Badge} cls=${o.kind === "error" ? "bad" : "warn"}>${o.text}<//>` : o.text}</span>`)}</div>`;
  }

  // ------------------------------------------------------------------ home
  function Home() {
    const [q, set] = useQuery({ days: "7", date: "" });
    const state = useData(urlWith("/admin/api/home", periodOf(q)));
    const msgs = (p) => urlWith("/admin/messages", p);
    const alerts = (p, type) => urlWith("/admin/alerts", { state: "all", ...p, ...(type ? { type } : {}) });
    return html`<${Loading} state=${state}>${(d) => {
      const t = d.totals, p = periodOf(q);
      const card = (n, label, href, extra) => html`<a class="card click" href=${href} onClick=${linkTo(href)}>
        <b>${n}</b><span class="muted">${label}</span>${extra}</a>`;
      return html`
      <div class="head"><div><h1>${BOT_NAME}</h1><div class="sub">What came in and what the bot did. Pick the days
        to look at; click any number to see those messages or alerts.</div>
        <div class="gap-top"><a href="/admin/alerts" onClick=${linkTo("/admin/alerts")}><b>${d.open_now} open
          alert${d.open_now === 1 ? "" : "s"}</b></a>${" "}${d.open_by_tier.map((x) => html`<a key=${x.tier}
          href=${"/admin/alerts?tier=" + x.tier} onClick=${linkTo("/admin/alerts?tier=" + x.tier)}><${Badge}
          cls=${x.tier}>${x.label}: ${x.n}<//></a> `)}</div></div>
        <div class="state"><${Badge} cls=${d.bot.state} dot>${{ running: "Running", paused: "Paused", down: "Not responding" }[d.bot.state]}<//>
          <div class="small muted">checked in ${d.bot.checkin.ago}</div></div></div>
      <div class="filters"><${Period} q=${q} set=${set} /></div>
      <h2 class="period">${d.period.label}</h2>
      <div class="cards">
        ${card(t.messages, "Messages", msgs(p), t.messages > 0 && html`<span class="small muted">${t.email} email · ${t.chat} chat</span>`)}
        ${card(t.problems, "Protocol problems found", alerts(p, "protocol"))}
        ${card(t.answered, "Questions answered", msgs({ ...p, did: "answered" }))}</div>
      ${t.errors > 0 && html`<div class="error gap-bottom"><a href=${msgs({ ...p, did: "error" })}
        onClick=${linkTo(msgs({ ...p, did: "error" }))}>${t.errors} message${t.errors === 1 ? "" : "s"} in this period could
        not be read by the bot</a></div>`}
      ${d.days.length > 0 && html`<div class="panel table"><h2 class="pad-h">Day by day</h2><table class="days">
        <thead><tr><th>Day</th><th>Messages</th><th>Protocol problems found</th>
          <th>Questions answered</th></tr></thead>
        <tbody>${d.days.map((x) => html`<tr key=${x.date} class=${x.messages || x.problems ? "" : "quiet"}>
          <td class="nowrap">${x.label}</td>
          <td><${Count} n=${x.messages} href=${msgs({ date: x.date })} />
            ${x.errors > 0 && html` <a href=${msgs({ date: x.date, did: "error" })} onClick=${linkTo(msgs({ date: x.date, did: "error" }))}><${Badge}
              cls="bad">${x.errors} not read<//></a>`}</td>
          <td><${Count} n=${x.problems} href=${alerts({ date: x.date }, "protocol")} /></td>
          <td><${Count} n=${x.answered} href=${msgs({ date: x.date, did: "answered" })} /></td></tr>`)}</tbody></table>
        ${d.capped && html`<div class="small muted pad">Showing the latest 90 days. Pick a day above to see an older one.</div>`}</div>`}
      <${Team} team=${d.team} />
      <div class="panel"><h2>Is anything wrong with the bot?</h2>${d.problems.length
        ? html`<ul class="problems">${d.problems.map((p, i) => html`<li key=${i}>${p.link
            ? html`<a href=${p.link} onClick=${p.link.startsWith("/admin") ? linkTo(p.link) : undefined}>${p.text}</a>`
            : p.text}</li>`)}</ul>`
        : html`<div class="status"><${Badge} cls="ok" dot>None<//> Nothing is stuck or failed.</div>`}</div>`; }}<//>`;
  }

  function Team({ team }) {
    const main = team[0];
    const [id, setId] = useState(main ? main.study : "");
    const s = team.find((x) => x.study === id);
    if (!s) return html`<div class="panel"><h2>Study team</h2><div class="error">No study files were found on the
      server, so the study team cannot be shown.</div></div>`;
    return html`<div class="panel table"><div class="team-head">
        <div><h2>Study team</h2><div class="small muted">${s.short_name} · ${s.title}</div>
          <div class="small">Sponsor: <b>${s.sponsor || "not set"}</b> · Sites: ${s.sites.join(", ") || "none listed"}</div></div>
        ${team.length > 1 && html`<${Select} value=${id} onChange=${setId}
          options=${team.map((x) => ({ value: x.study, label: x.study + " · " + x.short_name }))} />`}</div>
      ${s.people.length ? html`<table><thead><tr><th>Name</th><th>Role</th><th>Email</th><th>Sites</th></tr></thead>
        <tbody>${s.people.map((p, i) => html`<tr key=${i}><td>${p.name}</td><td>${p.roles.join(", ")}</td>
          <td>${p.email ? html`<a href=${"mailto:" + p.email}>${p.email}</a>` : html`<span class="muted">no email on file</span>`}</td>
          <td class="small">${p.sites}</td></tr>`)}</tbody></table>`
        : html`<div class="empty pad">No people are listed for this study.</div>`}
      <div class="small muted pad">Exactly as listed in the study's settings file on the server.</div></div>`;
  }

  // ------------------------------------------------------------------ messages
  function Messages({ options }) {
    const [q, set] = useQuery({ days: "7", date: "", channel: "", study: "", did: "", before: "" });
    const state = useData("/admin/api/messages?" + new URLSearchParams(q));
    const studies = [{ value: "", label: "All studies" }].concat(options.studies.map((s) => ({ value: s, label: s })));
    return html`
      <div class="head"><div><h1>Messages</h1><div class="sub">Every chat and email, newest first. Click one to
        see the whole conversation.</div></div></div>
      <div class="filters">
        <${Period} q=${q} set=${set} />
        <${Select} value=${q.channel} onChange=${(v) => set("channel", v)} options=${[{ value: "", label: "Chat and email" },
          { value: "teams", label: "Chat only" }, { value: "email", label: "Email only" }]} />
        <${Select} value=${q.study} onChange=${(v) => set("study", v)} options=${studies} />
        <${Select} value=${q.did} onChange=${(v) => set("did", v)} options=${[{ value: "", label: "Everything" },
          { value: "answered", label: "Questions answered" }, { value: "error", label: "Could not be read" }]} />
      </div>
      <${Loading} state=${state}>${(d) => html`<div class="panel table">${d.rows.length ? html`<table>
        <thead><tr><th>Received</th><th>From</th><th>Message</th><th>What the bot did</th></tr></thead>
        <tbody>${d.rows.map((m) => html`<tr key=${m.id} class="click" onClick=${() => go("/admin/messages/" + m.id)}>
          <td class="small nowrap"><${When} w=${m.received} /><div class="muted">${m.received.ago}</div></td>
          <td>${m.from}<div class="small muted">${m.where}</div></td>
          <td><a class="plain" href=${"/admin/messages/" + m.id} onClick=${linkTo("/admin/messages/" + m.id)}>
            <div class="clip">${m.summary}</div></a></td>
          <td><${Outcome} items=${m.outcome} /></td></tr>`)}</tbody></table>
        ${d.more && html`<a class="more" href="#" onClick=${(e) => { e.preventDefault();
          go(location.pathname + "?" + new URLSearchParams({ ...q, before: d.rows[d.rows.length - 1].id })); }}>Older messages</a>`}`
        : html`<div class="empty pad">No messages ${q.date ? "on this day" : "in this period"}. Try another day or "All time".</div>`}</div>`}<//>`;
  }

  function Clamp({ text, children }) {
    const [open, setOpen] = useState(false);
    const long = (text || "").length > 400;
    return html`<div class=${"text" + (long && !open ? " long" : "")}>${linkify(text || "")}</div>
      ${(long || children) && html`<div class="foot">
        ${long && html`<button onClick=${() => setOpen(!open)}>${open ? "Show less" : "Show all"}</button>`}${children}</div>`}`;
  }

  function Bubble({ e }) {
    if (e.small)
      return html`<div class="receipt"><div><b>${e.who}</b>${" "}<span class="muted">${e.kind} · <${When} w=${e.when} /></span></div>
        <div class="muted">${e.text}</div></div>`;
    return html`<div class=${"bubble " + e.side + (e.current ? " current" : "")}>
      <div class="meta"><b>${e.who}</b>
        <span>${e.side === "bot" ? e.kind : e.channel}</span>
        <span><${When} w=${e.when} /></span>
        ${e.side === "bot" && e.status !== "sent" && html`<${Badge} cls="warn">${e.status}<//>`}
        ${e.current && html`<${Badge} cls="protocol">This message<//>`}</div>
      ${e.subject && html`<div class="small"><b>${e.subject}</b></div>`}
      <${Clamp} text=${e.text}>
        ${e.side === "person" && !e.current && html`<a href=${"/admin/messages/" + e.message_id}
          onClick=${linkTo("/admin/messages/" + e.message_id)}>Open message ${e.message_id}</a>`}
        ${e.side === "person" && !e.current && e.outcome && html`<${Outcome} items=${e.outcome} />`}<//></div>`;
  }

  const placeOf = (s) => (s.channel === "email" ? "Emailed to " + s.to : "Posted in the " + s.to);

  function Sent({ s }) {
    return html`<div class=${"sent " + s.channel}>
      <div class="sent-head"><span class="pill">${s.channel === "email" ? "Email" : "Chat post"}</span>
        ${s.subject ? html`<b>${s.subject}</b>` : html`<b>${s.kind}</b>`}
        ${s.status !== "sent" && html`<${Badge} cls="warn">${s.status}<//>`}
        <div class="small muted">${s.channel === "email" ? "To " + s.to : "In the " + s.to} · <${When} w=${s.when} /></div></div>
      <${Clamp} text=${s.text} /></div>`;
  }

  function Notice({ e }) {
    const [open, setOpen] = useState(false);
    const a = e.alert;
    return html`<div class="notice">
      <div class="notice-line"><span class="notice-mark">!</span>
        <div class="grow">
          <div>${a ? html`<b>Alert #${a.id}</b> <${AlertTags} a=${a} />` : html`<b>${e.sent[0].kind}</b>`}</div>
          ${a && html`<div class="clip">${a.title}</div>`}
          <div class="small muted">${[...new Set(e.sent.map(placeOf))].join(" · ")} · <${When} w=${e.when} /></div></div>
        <button class="link nowrap" onClick=${() => setOpen(!open)}>${open ? "Hide" : "See what was sent"}</button></div>
      ${open && html`<div class="sent-list">${e.sent.map((s, i) => html`<${Sent} key=${i} s=${s} />`)}</div>`}</div>`;
  }

  function Message({ id }) {
    const state = useData("/admin/api/messages/" + id);
    return html`<a class="back" href="/admin/messages" onClick=${(e) => { e.preventDefault();
        history.length > 1 ? history.back() : go("/admin/messages"); }}>← Back to messages</a>
      <${Loading} state=${state}>${(d) => html`
      <div class="head"><div><h1>Message ${d.message.id}</h1><div class="sub">${[d.message.from, d.message.where,
        d.message.received.text].join(" · ")}</div></div>
        <${Badge} cls=${d.message.status === "Error" ? "bad" : d.message.status === "Done" ? "ok" : "warn"} dot>${d.message.status}<//></div>
      <div class="layout">
        <div class="panel"><h2>The whole conversation</h2>
          <div class="sub lead">People on the left, the bot's replies on the right. Alerts the bot sent elsewhere
            are the lines in between.</div>
          <div class="chat">${d.conversation.map((e, i) => e.side === "notice"
            ? html`<${Notice} key=${i} e=${e} />` : html`<${Bubble} key=${i} e=${e} />`)}</div></div>
        <div>
          <div class="panel"><h2>What the bot did</h2><${Outcome} items=${d.message.outcome} />
            ${d.answers.length > 0 && html`<div class="gap-top">${d.answers.map((a, i) => html`<div key=${i}>
              <a href=${a.link} target="_blank" rel="noopener">Answer details</a> <span class="small muted">${a.when.text}</span></div>`)}</div>`}</div>
          ${d.alerts.length > 0 && html`<div class="panel"><h2>Alerts</h2>${d.alerts.map((a) => html`<div key=${a.id}
            class="gap-bottom"><${AlertTags} a=${a} /> <span class="small muted">${a.state}</span>
            <div><a href=${a.link} target="_blank" rel="noopener">#${a.id}</a> ${a.title}</div>
            <div class="small gap-top"><span class="muted">Sent to:</span> <${SentTo} s=${a.sent} /></div></div>`)}</div>`}
          <div class="panel"><h2>Details</h2><dl class="facts">
            <dt>From</dt><dd>${d.message.from} <span class="muted small">${d.message.from_addr}</span></dd>
            <dt>Where</dt><dd>${d.message.where}</dd>
            ${d.message.to.length > 0 && html`<dt>To</dt><dd>${d.message.to.join(", ")}</dd>`}
            ${d.message.cc.length > 0 && html`<dt>Cc</dt><dd>${d.message.cc.join(", ")}</dd>`}
            ${d.message.sent.text && html`<dt>Sent</dt><dd>${d.message.sent.text}
              <span class="muted small">the sender's own clock, not checked</span></dd>`}
            <dt>Received by the bot</dt><dd>${d.message.received.text}</dd>
            ${d.message.attachments.length > 0 && html`<dt>Attachments</dt><dd>${d.message.attachments.map((a) => a.name).join(", ")}</dd>`}
            ${d.speed.seconds != null && html`<dt>Time the bot took to check it</dt><dd>${d.speed.seconds} seconds</dd>`}</dl>
            <${Technical} d=${d} /></div>
        </div></div>`}<//>`;
  }

  function Technical({ d }) {
    const [open, setOpen] = useState(false);
    return html`<div class="gap-top"><button class="link" onClick=${() => setOpen(!open)}>
        ${open ? "Hide technical details" : "Show technical details"}</button>
      ${open && html`<div class="gap-top small">
        ${d.speed.steps.length > 0 && html`<div class="muted">Time per step: ${d.speed.steps.map((s) => s.name + " " + s.seconds + " s").join(" · ")}</div>`}
        <table class="steps gap-top"><tbody>${d.log.map((r, i) => html`<tr key=${i}>
          <td class="muted nowrap">${r.when.text}</td><td>${r.what}</td></tr>`)}</tbody></table></div>`}</div>`;
  }

  // ------------------------------------------------------------------ alerts
  function Alerts({ options }) {
    const [q, set] = useQuery({ state: "open", days: "all", date: "", type: "", tier: "", person: "", before: "" });
    const state = useData("/admin/api/alerts?" + new URLSearchParams(q));
    return html`
      <div class="head"><div><h1>Alerts</h1><div class="sub">Problems the bot raised. Click one to see its message and
        conversation.</div></div></div>
      <div class="filters">
        <${Select} value=${q.state} onChange=${(v) => set("state", v)} options=${[{ value: "open", label: "Open alerts" },
          { value: "cleared", label: "Cleared" }, { value: "suppressed", label: "Disregarded" }, { value: "all", label: "All alerts" }]} />
        <${Select} value=${q.type} onChange=${(v) => set("type", v)} options=${options.types} />
        <${Select} value=${q.tier} onChange=${(v) => set("tier", v)} options=${options.tiers} />
        <${Select} value=${q.person} onChange=${(v) => set("person", v)} options=${options.people} />
        <${Period} q=${q} set=${set} />
      </div>
      <${Loading} state=${state}>${(d) => html`<div class="panel table">${d.rows.length ? html`<table>
        <thead><tr><th>Opened</th><th>Type and urgency</th><th>Problem</th><th>Site and participant</th>
          <th>Sent to</th><th>State</th></tr></thead>
        <tbody>${d.rows.map((a) => html`<tr key=${a.id} class="click" onClick=${() => go("/admin/messages/" + a.message_id)}>
          <td class="small nowrap"><${When} w=${a.opened} /><div class="muted">${a.opened.ago}</div></td>
          <td><div class="stack"><${Badge} cls=${a.type.kind}>${a.type.label}<//><${Badge} cls=${a.tier}>${a.tier_label}<//></div></td>
          <td><div class="clip">${a.title}</div>
            <a class="small" href=${a.link} target="_blank" rel="noopener" onClick=${(e) => e.stopPropagation()}>Alert #${a.id} details</a></td>
          <td>${a.site}<div class="small muted">${a.participant || "participant not named"}</div></td>
          <td><${SentTo} s=${a.sent} /></td>
          <td>${a.state}${!a.counts && html`<div class="small muted">does not count</div>`}</td></tr>`)}</tbody></table>
        ${d.more && html`<a class="more" href="#" onClick=${(e) => { e.preventDefault();
          go(location.pathname + "?" + new URLSearchParams({ ...q, before: d.rows[d.rows.length - 1].id })); }}>Older alerts</a>`}`
        : html`<div class="empty pad">No alerts here.</div>`}</div>`}<//>`;
  }

  // ------------------------------------------------------------------ people
  function People() {
    const state = useData("/admin/api/people");
    return html`<div class="head"><div><h1>Sign-ins and use</h1><div class="sub">Who has used the web pages and who has
      written to the bot.</div></div></div>
      <${Loading} state=${state}>${(d) => html`
      <div class="panel table"><table><thead><tr><th>Person</th><th>Role</th><th>Last web visit</th>
        <th>Messages to the bot</th><th>Emails from the bot</th></tr></thead>
        <tbody>${d.people.map((p, i) => html`<tr key=${i}><td>${p.name}<div class="small muted">${p.email}</div></td>
          <td class="small">${p.roles}<div class="muted">${p.studies}</div></td>
          <td class="small">${p.last_visit.at ? html`${p.last_visit.ago}<div class="muted">${p.pages} pages opened</div>`
            : html`<span class="muted">never</span>`}</td>
          <td class="small">${p.messages ? html`${p.messages}<div class="muted">last ${p.last_message.ago}</div>`
            : html`<span class="muted">none</span>`}</td>
          <td class="small">${p.emails || p.summaries ? html`${p.emails} about alerts${p.emails_copy
            ? html`<div class="muted">${p.emails_copy} of them as a copy</div>` : ""}${p.summaries
            ? html`<div class="muted">plus ${p.summaries} daily ${p.summaries === 1 ? "summary" : "summaries"}</div>` : ""}` : html`<span class="muted">none</span>`}</td></tr>`)}</tbody></table></div>
      <div class="grid2">
        <div class="panel"><h2>Others who visited the web pages</h2>${d.others.length ? html`<table><tbody>
          ${d.others.map((o, i) => html`<tr key=${i}><td>${o.email}</td><td class="small"><${When} w=${o.last_visit} /></td>
            <td>${o.pages} pages</td></tr>`)}</tbody></table>` : html`<div class="muted">Nobody else.</div>`}</div>
        <div class="panel"><h2>Latest sign-in events</h2>${d.events.length ? html`<table class="steps"><tbody>
          ${d.events.map((e, i) => html`<tr key=${i}><td class="muted">${e.when.text}</td><td>${e.who}</td><td>${e.what}</td></tr>`)}
          </tbody></table>` : html`<div class="muted">None yet.</div>`}</div>
      </div>`}<//>`;
  }

  // ------------------------------------------------------------------ patient memory
  const CHANGE_WORDS = { corrected: "Corrected", retired: "Retired", restored: "Restored", linked: "Linked",
                         forgotten: "Deleted (participant forgotten)" };

  function FactChange({ study, f, onDone }) {
    const [mode, setMode] = useState("");
    const [text, setText] = useState(f.text);
    const [who, setWho] = useState("");
    const [reason, setReason] = useState("");
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const send = () => {
      setBusy(true); setError("");
      fetch("/admin/api/memory/change", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Cormon-Admin": "1" },
        body: JSON.stringify({ study, fact: f.id, action: mode, reason, text, participant: who }) })
        .then((r) => r.json().then((d) => { if (!r.ok) throw new Error(d.error || "http " + r.status); }))
        .then(() => { setMode(""); onDone(); })
        .catch((e) => setError(e.message)).finally(() => setBusy(false));
    };
    if (!mode) return html`<div class="small actions">
      ${f.retired ? html`<button class="link" onClick=${() => setMode("restore")}>Restore</button>` : html`
        <button class="link" onClick=${() => setMode("correct")}>Correct</button>
        <button class="link" onClick=${() => setMode("retire")}>Retire</button>
        ${!f.participants.length && html`<button class="link" onClick=${() => setMode("link")}>Link to a participant</button>`}`}</div>`;
    return html`<div class="change">
      ${mode === "correct" && html`<label>Corrected fact<textarea rows="2" value=${text} onInput=${(e) => setText(e.target.value)} /></label>`}
      ${mode === "link" && html`<label>Participant's study code<input value=${who} onInput=${(e) => setWho(e.target.value)} /></label>`}
      <label>Reason (kept in the audit trail)<input value=${reason} onInput=${(e) => setReason(e.target.value)} /></label>
      ${error && html`<div class="error">${error}</div>`}
      <div><button class="primary" disabled=${busy} onClick=${send}>${{ correct: "Save the correction", retire: "Retire it",
        restore: "Restore it", link: "Link it" }[mode]}</button>
        <button class="link" onClick=${() => setMode("")}>Cancel</button></div></div>`;
  }

  function FactRow({ study, f, onDone }) {
    return html`<tr key=${f.id} class=${f.retired ? "retired" : ""}>
      <td class="small nowrap">${f.reported}<div class="muted">${f.who}</div></td>
      <td>${f.text}${f.happened && html`<div class="small muted">Happened: ${f.happened}</div>`}
        ${f.retired && html`<div class="small"><${Badge}>Retired<//> ${f.retired_why}</div>`}
        ${f.changes.map((c, i) => html`<div key=${i} class="small muted">${`${CHANGE_WORDS[c.what] || c.what} by ${c.by}, ${c.when}: `}
          ${c.reason}${c.what === "corrected" ? html` (was: "${c.old}")` : ""}</div>`)}
        <${FactChange} study=${study} f=${f} onDone=${onDone} /></td>
      <td class="small">${f.message_id ? html`<a href=${"/admin/messages/" + f.message_id}
        onClick=${linkTo("/admin/messages/" + f.message_id)}>Message ${f.message_id}</a>` : ""}
        ${f.alert_id ? html`<div class="muted">Alert #${f.alert_id}</div>` : ""}</td></tr>`;
  }

  function Memory() {
    const [q, set] = useQuery({ study: "", participant: "" });
    const [n, setN] = useState(0);
    const state = useData("/admin/api/memory?" + new URLSearchParams({ ...q, r: n }));
    const again = () => setN(n + 1);
    const facts = (rows, study) => html`<div class="panel table"><table>
      <thead><tr><th>Reported</th><th>Fact</th><th>From</th></tr></thead>
      <tbody>${rows.map((f) => html`<${FactRow} key=${f.id} study=${study} f=${f} onDone=${again} />`)}</tbody></table></div>`;
    return html`
      <div class="head"><div><h1>Patient memory</h1><div class="sub">What the bot remembers about each participant
        from earlier messages, and uses in its answers. Correct or retire a fact that is wrong; nothing is deleted,
        and every change is kept in the audit trail with its reason.</div></div></div>
      <${Loading} state=${state}>${(d) => html`
        <div class="filters">
          <${Select} value=${d.study} onChange=${(v) => set("study", v)} options=${d.studies.map((s) => ({ value: s, label: s }))} />
          ${d.participant && html`<a href="#" onClick=${(e) => { e.preventDefault(); set("participant", ""); }}>All participants</a>`}
        </div>
        ${!d.enabled ? html`<div class="empty">Patient memory is switched off (memory.enabled).</div>`
          : d.error ? html`<div class="error">${d.error}</div>`
          : d.participant ? html`<h2>${d.participant}</h2>${d.facts.length ? facts(d.facts, d.study)
            : html`<div class="empty">Nothing remembered about ${d.participant}.</div>`}`
          : html`
            <div class="panel table">${d.participants.length ? html`<table>
              <thead><tr><th>Participant</th><th>Facts</th><th>Last reported</th></tr></thead>
              <tbody>${d.participants.map((p) => html`<tr key=${p.key} class="click" onClick=${() => set("participant", p.id)}>
                <td>${p.id}</td><td>${p.facts}${p.retired ? html`<span class="muted"> (and ${p.retired} retired)</span>` : ""}</td>
                <td class="small">${p.last ? new Date(p.last).toLocaleDateString() : ""}</td></tr>`)}</tbody></table>`
              : html`<div class="empty pad">Nothing remembered yet.</div>`}</div>
            <h2 class="gap-top">Not linked to a participant</h2>
            <div class="sub">Facts from messages that gave no clear study code. Link one to its participant, or leave it.</div>
            ${d.unlinked.length ? facts(d.unlinked, d.study) : html`<div class="empty">None.</div>`}`}`}<//>`;
  }

  // ------------------------------------------------------------------ app
  function App() {
    const loc = useLocation();
    const opts = useData("/admin/api/options");
    const path = loc.split("?")[0].replace(/\/+$/, "") || "/admin";
    const msg = path.match(/^\/admin\/messages\/(\d+)$/);
    let page;
    if (path === "/admin") page = html`<${Home} />`;
    else if (msg) page = html`<${Message} key=${msg[1]} id=${msg[1]} />`;
    else if (path === "/admin/messages" || path === "/admin/alerts")
      page = html`<${Loading} state=${opts}>${(o) => path === "/admin/messages"
        ? html`<${Messages} key=${loc} options=${o} />` : html`<${Alerts} key=${loc} options=${o} />`}<//>`;
    else if (path === "/admin/people") page = html`<${People} />`;
    else if (path === "/admin/memory") page = html`<${Memory} key=${loc} />`;
    else page = html`<div class="error">Not found.</div>`;
    const section = msg ? "/admin/messages" : path;
    return html`<div class="app">
      <aside class="side"><div class="logo">${BOT_AVATAR ? html`<img src=${BOT_AVATAR} alt="" />` : html`<i></i>`}${BOT_NAME}</div>
        ${NAV.map(([to, name]) => html`<a key=${to} href=${to} class=${section === to ? "on" : ""} onClick=${linkTo(to)}>${name}</a>`)}
        <div class="who">${EMAIL}<br /><a href="/auth/logout">Sign out</a></div></aside>
      <main>${page}<div class="foot-note">Read-only, except corrections to patient memory, each kept with its reason.
        Every page view is logged. This monitor does not review safety
        information. This system does not guarantee the detection of safety signals. Silence does not constitute
        clearance.</div></main></div>`;
  }

  ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
})();
