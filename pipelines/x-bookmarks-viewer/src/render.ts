import type { Bookmark, Media, Tweet, User } from "./db";

export function esc(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Escape, then linkify URLs, @mentions and #hashtags. Preserves newlines. */
function richText(text: string): string {
  let html = esc(text);
  html = html.replace(
    /(https?:\/\/[^\s<]+)/g,
    (u) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`,
  );
  html = html.replace(
    /(^|[^\w@])@(\w{1,15})/g,
    (_m, pre, handle) =>
      `${pre}<a href="https://x.com/${handle}" target="_blank" rel="noopener noreferrer">@${handle}</a>`,
  );
  html = html.replace(
    /(^|\s)#(\w+)/g,
    (_m, pre, tag) =>
      `${pre}<a href="https://x.com/hashtag/${tag}" target="_blank" rel="noopener noreferrer">#${tag}</a>`,
  );
  return html.replace(/\n/g, "<br>");
}

function fmtDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function fmtCount(n: number | undefined): string {
  if (!n) return "";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, "") + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1).replace(/\.0$/, "") + "K";
  return String(n);
}

function avatar(user: User | null): string {
  if (user?.profile_image_url) {
    return `<img class="avatar" src="${esc(user.profile_image_url)}" alt="" loading="lazy">`;
  }
  const letter = (user?.name || user?.username || "?").trim().charAt(0).toUpperCase();
  // deterministic hue from the username so avatars stay stable
  const seed = (user?.username || user?.id || "x")
    .split("")
    .reduce((a, c) => a + c.charCodeAt(0), 0);
  const hue = seed % 360;
  return `<div class="avatar placeholder" style="background:hsl(${hue} 55% 45%)">${esc(letter)}</div>`;
}

function authorLine(user: User | null, authorId: string | null): string {
  const name = user?.name ?? "Unknown";
  const handle = user?.username ?? authorId ?? "";
  const href = user?.username ? `https://x.com/${user.username}` : "#";
  return `
    <a class="author" href="${esc(href)}" target="_blank" rel="noopener noreferrer">
      <span class="name">${esc(name)}</span>
      <span class="handle">@${esc(handle)}</span>
    </a>`;
}

function mediaGrid(media: Media[]): string {
  if (!media.length) return "";
  const items = media
    .map((m) => {
      if (m.type === "photo" && m.url) {
        return `<a href="${esc(m.url)}" target="_blank" rel="noopener"><img src="${esc(m.url)}" loading="lazy" alt=""></a>`;
      }
      if ((m.type === "video" || m.type === "animated_gif") && m.url) {
        const poster = m.preview_image_url ? ` poster="${esc(m.preview_image_url)}"` : "";
        const auto = m.type === "animated_gif" ? " autoplay loop muted" : "";
        return `<video controls playsinline${poster}${auto} preload="none"><source src="${esc(m.url)}"></video>`;
      }
      if (m.preview_image_url) {
        return `<img src="${esc(m.preview_image_url)}" loading="lazy" alt="">`;
      }
      return "";
    })
    .join("");
  const cls = media.length === 1 ? "media one" : "media";
  return `<div class="${cls}">${items}</div>`;
}

function tweetUrl(t: Tweet): string {
  const handle = t.author?.username ?? "i";
  return `https://x.com/${handle}/status/${t.id}`;
}

function metricsLine(t: Tweet): string {
  const m = t.metrics || {};
  const parts = [
    ["💬", m.reply_count],
    ["🔁", m.retweet_count],
    ["♥", m.like_count],
    ["🔖", m.bookmark_count],
  ]
    .filter(([, n]) => n)
    .map(([icon, n]) => `<span>${icon} ${fmtCount(n as number)}</span>`)
    .join("");
  const views = t.metrics.impression_count
    ? `<span class="views">${fmtCount(t.metrics.impression_count)} views</span>`
    : "";
  return `<div class="metrics">${parts}${views}</div>`;
}

/** A nested quoted tweet (lighter card, no metrics). */
function quotedCard(t: Tweet): string {
  return `
    <a class="quoted" href="${tweetUrl(t)}" target="_blank" rel="noopener noreferrer">
      <div class="quoted-head">
        <span class="name">${esc(t.author?.name ?? "Unknown")}</span>
        <span class="handle">@${esc(t.author?.username ?? "")}</span>
      </div>
      <div class="text">${richText(t.text)}</div>
      ${mediaGrid(t.media)}
    </a>`;
}

function threadReply(t: Tweet): string {
  return `
    <div class="reply">
      <div class="text">${richText(t.text)}</div>
      ${mediaGrid(t.media)}
    </div>`;
}

function searchCorpus(b: Bookmark): string {
  const t = b.tweet;
  const parts = [
    t.author?.name,
    t.author?.username,
    t.text,
    fmtDate(t.created_at),
    b.quoted?.author?.name,
    b.quoted?.author?.username,
    b.quoted?.text,
    ...b.thread.map((r) => r.text),
  ];
  return parts.filter(Boolean).join(" ").replace(/\s+/g, " ").toLowerCase();
}

export function bookmarkCard(b: Bookmark): string {
  const t = b.tweet;
  return `
  <article class="tweet" data-search="${esc(searchCorpus(b))}">
    <div class="left">${avatar(t.author)}</div>
    <div class="body">
      <div class="head">
        ${authorLine(t.author, null)}
        <a class="ts" href="${tweetUrl(t)}" target="_blank" rel="noopener noreferrer">${fmtDate(t.created_at)}</a>
      </div>
      <div class="text">${richText(t.text)}</div>
      ${mediaGrid(t.media)}
      ${b.quoted ? quotedCard(b.quoted) : ""}
      ${metricsLine(t)}
      ${
        b.thread.length
          ? `<details class="thread"><summary>Show thread (${b.thread.length})</summary>${b.thread
              .map(threadReply)
              .join("")}</details>`
          : ""
      }
    </div>
  </article>`;
}

// Client-side search: filters cards by an AND of whitespace tokens against
// each card's data-search corpus, and highlights matches within the visible
// text fields only (walks text nodes so anchors/markup stay intact).
const SEARCH_SCRIPT = String.raw`
(function () {
  var input = document.getElementById("q");
  var countEl = document.getElementById("count");
  var emptyEl = document.getElementById("empty");
  if (!input) return;
  var cards = Array.prototype.slice.call(document.querySelectorAll(".tweet"));
  var baseCount = countEl ? countEl.textContent : "";
  // selectors of the text-bearing fields we highlight inside each card
  var HL = ".name,.handle,.ts,.text";

  function esc(s) { return s.replace(/[.*+?^$\{\}()|[\]\\]/g, "\\$&"); }

  function clearMarks(root) {
    var marks = root.querySelectorAll("mark.hl");
    for (var i = 0; i < marks.length; i++) {
      var m = marks[i];
      m.parentNode.replaceChild(document.createTextNode(m.textContent), m);
    }
    if (marks.length) root.normalize();
  }

  function highlight(root, re) {
    var fields = root.querySelectorAll(HL);
    for (var f = 0; f < fields.length; f++) {
      var walker = document.createTreeWalker(fields[f], NodeFilter.SHOW_TEXT, null);
      var nodes = [], n;
      while ((n = walker.nextNode())) nodes.push(n);
      for (var j = 0; j < nodes.length; j++) {
        var node = nodes[j], text = node.nodeValue;
        re.lastIndex = 0;
        if (!re.test(text)) continue;
        re.lastIndex = 0;
        var frag = document.createDocumentFragment(), last = 0, mm;
        while ((mm = re.exec(text))) {
          if (mm.index > last) frag.appendChild(document.createTextNode(text.slice(last, mm.index)));
          var mark = document.createElement("mark");
          mark.className = "hl";
          mark.textContent = mm[0];
          frag.appendChild(mark);
          last = mm.index + mm[0].length;
          if (mm.index === re.lastIndex) re.lastIndex++;
        }
        if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
        node.parentNode.replaceChild(frag, node);
      }
    }
  }

  var raf = 0;
  function run() {
    var q = input.value.trim().toLowerCase();
    // strip a leading "@" so "@handle" matches the bare handle in the corpus
    var tokens = q
      ? q.split(/\s+/).map(function (t) { return t.replace(/^@/, ""); }).filter(Boolean)
      : [];
    var re = tokens.length
      ? new RegExp("(" + tokens.map(esc).join("|") + ")", "gi")
      : null;
    var shown = 0;
    for (var i = 0; i < cards.length; i++) {
      var card = cards[i];
      var hay = card.getAttribute("data-search") || "";
      var match = tokens.every(function (t) { return hay.indexOf(t) !== -1; });
      card.hidden = !match;
      clearMarks(card);
      if (match) {
        shown++;
        if (re) highlight(card, re);
      }
    }
    if (countEl) countEl.textContent = q ? shown + " of " + cards.length + " match" : baseCount;
    if (emptyEl) emptyEl.className = (q && shown === 0) ? "show" : "";
  }

  input.addEventListener("input", function () {
    if (raf) cancelAnimationFrame(raf);
    raf = requestAnimationFrame(run);
  });
  // Esc clears
  input.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { input.value = ""; run(); }
  });
})();
`;

export function page(bookmarks: Bookmark[], counts: Record<string, number>): string {
  const cards = bookmarks.map(bookmarkCard).join("\n");
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- twimg CDN 403s any request carrying a Referer (hotlink protection);
     suppress it so images/videos load like an unauthenticated request. -->
<meta name="referrer" content="no-referrer">
<title>X Bookmarks</title>
<style>
  :root {
    --bg:#000; --card:#16181c; --border:#2f3336; --text:#e7e9ea;
    --muted:#71767b; --accent:#1d9bf0;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { position:sticky; top:0; z-index:5; backdrop-filter:blur(12px);
    background:rgba(0,0,0,.65); border-bottom:1px solid var(--border);
    padding:10px 16px; display:flex; align-items:center; gap:12px;
    flex-wrap:wrap; }
  header h1 { font-size:19px; margin:0; font-weight:800; }
  header .sub { color:var(--muted); font-size:13px; white-space:nowrap; }
  #q { flex:1 1 220px; min-width:0; background:#202327; color:var(--text);
    border:1px solid var(--border); border-radius:9999px; padding:8px 14px;
    font-size:14px; outline:none; }
  #q:focus { border-color:var(--accent); }
  #q::placeholder { color:var(--muted); }
  mark.hl { background:#ffd60055; color:inherit; border-radius:2px; }
  .tweet[hidden] { display:none; }
  #empty { display:none; padding:32px 16px; text-align:center; color:var(--muted); }
  #empty.show { display:block; }
  main { max-width:600px; margin:0 auto; }
  .tweet { display:flex; gap:12px; padding:12px 16px;
    border-bottom:1px solid var(--border); }
  .left { flex:0 0 auto; }
  .avatar { width:44px; height:44px; border-radius:50%; object-fit:cover;
    display:flex; align-items:center; justify-content:center;
    font-weight:700; color:#fff; font-size:18px; }
  .body { flex:1 1 auto; min-width:0; }
  .head { display:flex; align-items:baseline; gap:6px; flex-wrap:wrap; }
  .author { text-decoration:none; color:inherit; display:flex; gap:6px;
    align-items:baseline; flex-wrap:wrap; }
  .name { font-weight:700; color:var(--text); }
  .author:hover .name { text-decoration:underline; }
  .handle { color:var(--muted); }
  .ts { margin-left:auto; color:var(--muted); text-decoration:none; font-size:14px; }
  .ts:hover { text-decoration:underline; }
  .text { margin:2px 0 8px; white-space:normal; word-wrap:break-word; }
  .text a, header a { color:var(--accent); text-decoration:none; }
  .text a:hover { text-decoration:underline; }
  .media { display:grid; grid-template-columns:1fr 1fr; gap:2px; margin:8px 0;
    border-radius:16px; overflow:hidden; border:1px solid var(--border); }
  .media.one { grid-template-columns:1fr; }
  .media img, .media video { width:100%; height:100%; max-height:510px;
    object-fit:cover; display:block; }
  .quoted { display:block; text-decoration:none; color:inherit;
    border:1px solid var(--border); border-radius:14px; padding:10px 12px;
    margin:8px 0; }
  .quoted:hover { background:#1c1f23; }
  .quoted-head { display:flex; gap:6px; align-items:baseline; margin-bottom:2px; }
  .quoted .media { margin:6px 0 0; }
  .metrics { display:flex; gap:18px; color:var(--muted); font-size:13px;
    margin-top:6px; flex-wrap:wrap; align-items:center; }
  .metrics .views { margin-left:auto; }
  .thread { margin-top:8px; }
  .thread summary { color:var(--accent); cursor:pointer; font-size:14px;
    list-style:none; }
  .thread summary::-webkit-details-marker { display:none; }
  .reply { border-left:2px solid var(--border); padding:6px 0 6px 12px;
    margin:8px 0 0; }
  footer { text-align:center; color:var(--muted); padding:24px; font-size:13px; }
</style>
</head>
<body>
  <header>
    <h1>Bookmarks</h1>
    <input id="q" type="search" autocomplete="off" autofocus
           placeholder="Search name, @handle, text, date…">
    <span class="sub" id="count">${counts.bookmarks} bookmarks · ${counts.users} authors · ${counts.media} media</span>
  </header>
  <main>
    ${cards || '<p style="padding:24px;color:var(--muted)">No bookmarks yet.</p>'}
    <div id="empty">No bookmarks match your search.</div>
  </main>
  <footer>read-only mirror of your X bookmarks · pipeline-dbos</footer>
  <script>${SEARCH_SCRIPT}</script>
</body>
</html>`;
}
