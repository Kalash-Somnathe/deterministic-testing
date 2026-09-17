/* Deterministic Testing — visualiser
 *
 * The browser runs no simulation. Everything drawn here was produced by
 * scripts/export_web_data.py running the real framework, or copied verbatim from
 * the committed result files in artifacts/. This file only reads evidence.
 */
(function () {
  "use strict";

  var SVGNS = "http://www.w3.org/2000/svg";
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  var STATE = {
    manifest: null,
    featured: null,
    banks: {},          // variant -> {traces: {seed: trace}}
    bankLoading: {},
    trace: null,
    cursor: 0,
    axis: "time",
    playing: 0,
    variant: "buggy",
    seed: 1,
    story: "planted",
    nodes: [],
    reduce: window.matchMedia("(prefers-reduced-motion: reduce)").matches
  };

  /* ── small helpers ────────────────────────────────────────────── */

  function svg(tag, attrs, text) {
    var node = document.createElementNS(SVGNS, tag);
    if (attrs) for (var k in attrs) if (attrs[k] !== null && attrs[k] !== undefined) node.setAttribute(k, attrs[k]);
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function elem(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function num(n) { return Number(n).toLocaleString("en-US"); }
  /** Greedy word wrap into lines of at most `cols` characters. SVG text does not
   *  wrap on its own, and a label that runs under the next column is worse than a
   *  label on two lines. */
  function wrap(text, cols) {
    var out = [], line = "";
    text.split(" ").forEach(function (word) {
      if (line && (line + " " + word).length > cols) { out.push(line); line = word; }
      else { line = line ? line + " " + word : word; }
    });
    if (line) out.push(line);
    return out;
  }
  function pct(n, d) { return d ? ((n / d) * 100).toFixed(1).replace(/\.0$/, ".0") + "%" : "0%"; }

  /** Virtual time is an integer microsecond count. Print it the way the
   *  framework prints it, so the page and the CLI agree. */
  function us(v) {
    if (v === 0) return "0";
    if (Math.abs(v) < 1000) return v + " µs";
    if (Math.abs(v) < 1000000) return (v / 1000).toFixed(v % 1000 ? 3 : 0).replace(/\.?0+$/, "") + " ms";
    return (v / 1000000).toFixed(6).replace(/0+$/, "").replace(/\.$/, "") + " s";
  }
  function tstamp(v) { return "t=" + (v / 1e6).toFixed(6) + "s"; }

  /* ── theme ────────────────────────────────────────────────────── */

  (function theme() {
    var order = ["auto", "light", "dark"];
    var btn = $("#theme");
    var label = $("[data-theme-label]", btn);
    var saved = null;
    try { saved = localStorage.getItem("deterministic-testing-theme"); } catch (e) { /* private mode */ }
    var mode = order.indexOf(saved) >= 0 ? saved : "auto";
    function apply() {
      document.documentElement.setAttribute("data-theme", mode);
      label.textContent = mode;
      btn.setAttribute("aria-label", "Colour theme: " + mode + ". Click to change.");
    }
    btn.addEventListener("click", function () {
      mode = order[(order.indexOf(mode) + 1) % order.length];
      try { localStorage.setItem("deterministic-testing-theme", mode); } catch (e) { /* ignore */ }
      apply();
      redrawAll();
    });
    apply();
  })();

  /* ── event glosses: the part that makes a trace legible ───────── */

  var TONE = {
    SEND: "msg", DELIVER: "msg", RECV: "msg",
    DROP: "fault", DELAY: "fault", DUPLICATE: "fault",
    VIOLATION: "breach"
  };

  var WHY = {
    DROP: "The sender is not told. It will sit on a timeout, and something upstream will retry.",
    DELAY: "Delivery is pushed past a timeout, so a retry and the original answer are now both in flight.",
    DUPLICATE: "A second copy of an already-answered request is now sitting in a mailbox, waiting to be mistaken for a fresh reply.",
    TIMEOUT: "A timeout is a decision made on missing information. It is where retries, and duplicate work, begin.",
    RECV: "Taking a message out of the mailbox costs a scheduler step, on purpose: checking the mailbox is exactly where check-then-act bugs live."
  };

  function msgOf(trace, seq) { return trace.messages[String(seq)] || null; }

  function label(msg) {
    if (!msg) return "";
    return msg.op + (msg.key ? "(" + msg.key + ")" : "");
  }

  function gloss(trace, i) {
    var e = trace.events[i];
    var m = e.seq !== undefined ? msgOf(trace, e.seq) : null;
    var who = "<b>" + e.proc + "</b>";
    switch (e.kind) {
      case "SPAWN": return who + " starts.";
      case "EXIT": return who + " has nothing left to do and shuts down.";
      case "SLEEP": return who + " sleeps for " + us(e.log ? e.log.for_us : 0) + " of simulated time — which costs no wall clock at all.";
      case "WAKE": return who + " wakes.";
      case "TIMEOUT": return who + " gives up waiting and takes the timeout branch.";
      case "LOG": return who + " logs " + JSON.stringify(e.log || {}) + ".";
      case "SEND":
        return who + " sends <b>" + label(m) + "</b> to <b>" + (m ? m.to : "?") + "</b> as message #" + e.seq + ".";
      case "DELIVER":
        return "Message #" + e.seq + " (<b>" + label(m) + "</b>) reaches " + who + "'s mailbox.";
      case "RECV":
        return who + " takes <b>" + label(m) + "</b> out of its mailbox.";
      case "DROP":
        return "The network <b>drops</b> message #" + e.seq +
          (m ? " (" + label(m) + ", " + m.from + " → " + m.to + ")" : "") + ". It never arrives.";
      case "DELAY":
        return "The network <b>holds message #" + e.seq + " back</b> by " + us(e.value) +
          (m ? " (" + label(m) + ", " + m.from + " → " + m.to + ")" : "") + ".";
      case "DUPLICATE":
        return "The network <b>duplicates</b> message #" + e.seq + " into " + e.value + " copies" +
          (m ? " (" + label(m) + ", " + m.from + " → " + m.to + ")" : "") + ".";
      case "VIOLATION":
        return "<b>" + (e.violation ? e.violation.invariant : "invariant") + "</b> — " +
          (e.violation ? e.violation.detail : "");
      default: return e.kind + " on " + e.proc + ".";
    }
  }

  /* ── the tape: lanes, arrows, faults, the violation ───────────── */

  var PAD = { l: 84, r: 26, t: 30, b: 30 };
  var LANE_H = 44;
  var GAP_US = 20000;      // above this, the clock jump is drawn as an axis break
  var BREAK_W = 38;

  function buildScale(trace, width) {
    var avail = Math.max(220, width - PAD.l - PAD.r);
    var events = trace.events;
    var xs = {}, breaks = [];

    if (STATE.axis === "step") {
      var col = Math.max(7, Math.min(26, avail / Math.max(1, events.length - 1)));
      var byIndex = events.map(function (_, i) { return PAD.l + i * col; });
      return { byIndex: byIndex, breaks: [], width: PAD.l + PAD.r + col * Math.max(1, events.length - 1), ticks: stepTicks(trace, byIndex) };
    }

    var times = [];
    var seen = Object.create(null);
    events.forEach(function (e) { if (!(e.t in seen)) { seen[e.t] = 1; times.push(e.t); } });
    times.sort(function (a, b) { return a - b; });

    /* Virtual time is wildly non-uniform: microseconds of message passing, then a
       half-second retry timeout, then thirty idle seconds. A linear axis would
       collapse the interesting part to a smear. Segment widths are therefore a
       compressive power of the elapsed gap -- still strictly monotone in time, so
       left is always earlier, but a 2-second wait is drawn a few times wider than a
       1-millisecond hop rather than two thousand times wider. The largest jumps are
       called out explicitly underneath so the compression is never a silent lie. */
    var segs = [], total = 0, i, gap;
    for (i = 1; i < times.length; i++) {
      gap = times[i] - times[i - 1];
      var w = 6 + 22 * Math.pow(Math.max(gap, 1) / 1000, 0.22);
      segs.push({ w: w, gap: gap });
      total += w;
    }
    var k = total > 0 ? avail / total : 1;
    k = Math.max(0.35, Math.min(3, k));

    var x = PAD.l;
    xs[times[0]] = x;
    for (var j = 0; j < segs.length; j++) {
      segs[j].x0 = x;
      x += Math.max(5, segs[j].w * k);
      segs[j].x1 = x;
      xs[times[j + 1]] = x;
    }

    // Call out only the handful of genuinely large clock jumps, and never two so
    // close together that their labels collide.
    breaks = [];
    segs.slice()
      .filter(function (s) { return s.gap > GAP_US; })
      .sort(function (a, b) { return b.gap - a.gap; })
      .forEach(function (s) {
        if (breaks.length >= 4) return;
        for (var q = 0; q < breaks.length; q++) if (Math.abs(breaks[q].x0 - s.x0) < 150) return;
        breaks.push(s);
      });
    breaks.sort(function (a, b) { return a.x0 - b.x0; });

    return {
      byIndex: events.map(function (e) { return xs[e.t]; }),
      breaks: breaks,
      width: x + PAD.r,
      ticks: timeTicks(times, xs)
    };
  }

  function timeTicks(times, xs) {
    // Evenly spaced in drawn distance, never closer than 76px.
    var out = [], last = -1e9;
    times.forEach(function (t) {
      var x = xs[t];
      if (x - last < 76) return;
      last = x;
      out.push({ x: x, text: (t / 1e6).toFixed(3) + "s" });
    });
    var end = times[times.length - 1];
    if (out.length && xs[end] - out[out.length - 1].x > 40) {
      out.push({ x: xs[end], text: (end / 1e6).toFixed(3) + "s" });
    }
    return out;
  }

  function stepTicks(trace, byIndex) {
    var out = [], every = Math.max(1, Math.round(trace.events.length / 8));
    for (var i = 0; i < trace.events.length; i += every) {
      out.push({ x: byIndex[i], text: "#" + trace.events[i].step });
    }
    return out;
  }

  function renderTape() {
    var trace = STATE.trace, host = $("#tape"), shell = $("#tape-scroll");
    if (!trace) return;
    while (host.firstChild) host.removeChild(host.firstChild);

    var scale = buildScale(trace, shell.clientWidth || 900);
    var lanes = trace.lanes;
    var laneY = {};
    lanes.forEach(function (name, i) { laneY[name] = PAD.t + i * LANE_H + LANE_H / 2; });
    var violationY = PAD.t + lanes.length * LANE_H + 10;
    var height = PAD.t + lanes.length * LANE_H + PAD.b;
    var W = Math.max(scale.width, shell.clientWidth || 900);

    host.setAttribute("width", W);
    host.setAttribute("height", height);
    host.setAttribute("viewBox", "0 0 " + W + " " + height);
    host.setAttribute("aria-label",
      "Trace for seed " + trace.seed + ", " + trace.variant + " consumer: " +
      trace.eventCount + " events across " + lanes.length + " processes" +
      (trace.violation ? ", ending in a violation of “" + trace.violation.name + "”" : ""));

    var gBg = svg("g"), gMsg = svg("g"), gNode = svg("g"), gTop = svg("g");

    /* lanes */
    lanes.forEach(function (name, i) {
      if (i % 2 === 0) gBg.appendChild(svg("rect", { x: 0, y: PAD.t + i * LANE_H, width: W, height: LANE_H, "class": "lane-band" }));
      gBg.appendChild(svg("line", { x1: PAD.l - 12, y1: laneY[name], x2: W - PAD.r + 12, y2: laneY[name], "class": "lane-rule" }));
      gBg.appendChild(svg("text", { x: PAD.l - 20, y: laneY[name] + 4, "class": "lane-label", "text-anchor": "end" }, name));
    });

    /* axis */
    scale.ticks.forEach(function (t) {
      gBg.appendChild(svg("line", { x1: t.x, y1: PAD.t - 8, x2: t.x, y2: PAD.t, "class": "axis-tick" }));
      gBg.appendChild(svg("text", { x: t.x, y: PAD.t - 12, "class": "axis-text", "text-anchor": "middle" }, t.text));
    });
    scale.breaks.forEach(function (b) {
      var mid = (b.x0 + b.x1) / 2, top = PAD.t + 2, bot = PAD.t + lanes.length * LANE_H - 2;
      gBg.appendChild(svg("line", { x1: mid, y1: top, x2: mid, y2: bot, "class": "gap-break" }));
      gBg.appendChild(svg("text", { x: mid, y: bot + 14, "class": "gap-text", "text-anchor": "middle" },
        "clock jumps " + us(b.gap)));
    });

    /* node positions, with collision spreading for simultaneous events */
    var occupancy = Object.create(null);
    var nodes = trace.events.map(function (e, i) {
      var y = e.proc === "invariant" ? violationY : (laneY[e.proc] !== undefined ? laneY[e.proc] : violationY);
      var x = scale.byIndex[i];
      var key = Math.round(x) + ":" + Math.round(y);
      var n = occupancy[key] || 0;
      occupancy[key] = n + 1;
      return { x: x + n * 5, y: y, e: e, i: i };
    });
    STATE.nodes = nodes;

    /* faults, indexed by the message they hit */
    var faultOf = Object.create(null);
    trace.events.forEach(function (e) {
      if (e.kind === "DROP" || e.kind === "DELAY" || e.kind === "DUPLICATE") faultOf[e.seq] = e.kind;
    });
    var deliversOf = Object.create(null);
    trace.events.forEach(function (e, i) {
      if (e.kind === "DELIVER") (deliversOf[e.seq] = deliversOf[e.seq] || []).push(i);
    });

    var cur = STATE.cursor;
    // Nothing is dimmed until the reader starts stepping: the whole trace should be
    // legible as one shape on arrival. Dimming is a reading aid, not a reveal.
    var dim = cur > 0;
    var labelRight = Object.create(null);

    trace.events.forEach(function (e, i) {
      if (e.kind !== "SEND") return;
      var from = nodes[i];
      var arrivals = deliversOf[e.seq] || [];
      var fault = faultOf[e.seq];
      var future = dim && i > cur;
      var m = msgOf(trace, e.seq);
      var toY = m && laneY[m.to] !== undefined ? laneY[m.to] : from.y;

      if (arrivals.length === 0) {
        // dropped: a stub that stops, and an x where it stopped
        var stubX = from.x + 26, midY = (from.y + toY) / 2;
        var g = svg("g", { "class": "msg faulty" + (future ? " future" : "") });
        g.appendChild(svg("path", { d: "M" + from.x + " " + from.y + " L" + stubX + " " + midY, "class": "msg faulty" }));
        g.appendChild(svg("path", {
          d: "M" + (stubX - 4) + " " + (midY - 4) + " l8 8 M" + (stubX + 4) + " " + (midY - 4) + " l-8 8",
          "class": "glyph-drop"
        }));
        gMsg.appendChild(g);
      } else {
        arrivals.forEach(function (di, copy) {
          var to = nodes[di];
          var cls = "msg" + (fault === "DELAY" ? " delayed" : "") + (fault === "DUPLICATE" ? " faulty" : "");
          if (future || (dim && di > cur)) cls += " future";
          var dx = to.x - from.x;
          var bend = Math.min(26, Math.max(8, Math.abs(dx) * 0.22)) * (copy ? -1 : 1);
          var d = "M" + from.x + " " + from.y + " C" + (from.x + dx * 0.35) + " " + (from.y + bend) +
            " " + (from.x + dx * 0.65) + " " + (to.y - bend) + " " + to.x + " " + to.y;
          gMsg.appendChild(svg("path", { d: d, "class": cls }));
          // arrowhead
          var dir = to.y >= from.y ? 1 : -1;
          gMsg.appendChild(svg("path", {
            d: "M" + to.x + " " + to.y + " l-3.4 " + (-4.6 * dir) + " l6.8 0 z",
            "class": cls.replace("msg", "msg node") + (fault ? " faulty" : ""),
            fill: "currentColor",
            style: "fill:" + (fault ? "var(--fault)" : "var(--ink-3)") + ";stroke:none"
          }));
        });
      }

      // op label, only where there is room on that lane
      if (m && m.op) {
        var last = labelRight[from.y] || 0;
        var text = m.op;
        var wEst = text.length * 6.2 + 6;
        if (from.x > last + 8 && from.x + wEst < W - PAD.r) {
          gTop.appendChild(svg("text", {
            x: from.x + 3, y: from.y - 8, "class": "op-text" + (future ? " future" : "")
          }, text));
          labelRight[from.y] = from.x + wEst;
        }
      }
    });

    /* event nodes + hit targets */
    nodes.forEach(function (n) {
      var e = n.e, cls = "node";
      if (e.kind === "DROP" || e.kind === "DELAY" || e.kind === "DUPLICATE") cls += " fault";
      else if (e.quiet) cls += " quiet";
      if (dim && n.i > cur) cls += " future";
      if (e.kind === "DUPLICATE") {
        gNode.appendChild(svg("rect", { x: n.x - 4, y: n.y - 3, width: 8, height: 6, "class": cls + " glyph-dup" }));
      } else if (e.kind === "TIMEOUT") {
        gNode.appendChild(svg("circle", { cx: n.x, cy: n.y, r: 4.2, "class": cls + " glyph-timeout" }));
      } else if (e.kind !== "VIOLATION") {
        gNode.appendChild(svg("circle", { cx: n.x, cy: n.y, r: e.quiet ? 1.8 : 2.8, "class": cls }));
      }
      var hit = svg("rect", { x: n.x - 7, y: n.y - 13, width: 14, height: 26, "class": "hit" });
      hit.addEventListener("click", function () { setCursor(n.i); });
      gNode.appendChild(hit);
    });

    /* the violation: the one moment the whole page exists to show */
    var vIndex = -1;
    trace.events.forEach(function (e, i) { if (e.kind === "VIOLATION") vIndex = i; });
    if (vIndex >= 0) {
      var vx = nodes[vIndex].x;
      gTop.appendChild(svg("line", { x1: vx, y1: PAD.t - 4, x2: vx, y2: PAD.t + lanes.length * LANE_H + 4, "class": "breach-rule" }));
      var fw = 78;
      var fx = Math.min(vx + 4, W - fw - 4);
      gTop.appendChild(svg("rect", { x: fx, y: PAD.t - 4, width: fw, height: 15, "class": "breach-flag", rx: 2 }));
      gTop.appendChild(svg("text", { x: fx + 6, y: PAD.t + 7, "class": "breach-text" }, "VIOLATION"));
    }

    /* the probe cursor */
    if (nodes[cur]) {
      var c = nodes[cur];
      gTop.appendChild(svg("line", { x1: c.x, y1: PAD.t - 4, x2: c.x, y2: PAD.t + lanes.length * LANE_H + 4, "class": "probe-rule" }));
      gTop.appendChild(svg("circle", { cx: c.x, cy: c.y, r: 9, "class": "probe-halo" }));
      gTop.appendChild(svg("circle", { cx: c.x, cy: c.y, r: 3.6, "class": "probe-dot" }));
    }

    host.appendChild(gBg); host.appendChild(gMsg); host.appendChild(gNode); host.appendChild(gTop);

    // keep the cursor in view when stepping past the right edge
    if (nodes[cur]) {
      var cx = nodes[cur].x;
      if (cx < shell.scrollLeft + 60 || cx > shell.scrollLeft + shell.clientWidth - 60) {
        shell.scrollLeft = Math.max(0, cx - shell.clientWidth / 2);
      }
    }
  }

  /* ── the detail panel ─────────────────────────────────────────── */

  function renderDetail() {
    var host = $("#detail"), trace = STATE.trace;
    host.innerHTML = "";
    if (!trace) return;
    var i = STATE.cursor, e = trace.events[i];
    if (!e) return;

    var stamp = elem("div", "d-stamp");
    stamp.appendChild(elem("div", null, "step " + e.step));
    stamp.appendChild(elem("div", null, tstamp(e.t)));

    var kind = elem("span", "d-kind", e.kind);
    kind.setAttribute("data-tone", TONE[e.kind] || "");

    var g = elem("div", "d-gloss");
    g.innerHTML = gloss(trace, i);

    host.appendChild(stamp);
    host.appendChild(kind);
    host.appendChild(g);

    var m = e.seq !== undefined ? msgOf(trace, e.seq) : null;
    if (m && m.payload) {
      var pre = elem("div", "d-payload", JSON.stringify(m.payload));
      host.appendChild(pre);
    }
    if (WHY[e.kind]) {
      host.appendChild(elem("div", "d-why", WHY[e.kind]));
    }

    $("#scrub").value = i;
    $("#scrub-out").textContent = (i + 1) + " / " + trace.events.length;
  }

  function setCursor(i) {
    if (!STATE.trace) return;
    var n = STATE.trace.events.length;
    STATE.cursor = Math.max(0, Math.min(n - 1, i));
    renderTape();
    renderDetail();
  }

  function loadTrace(trace) {
    STATE.trace = trace;
    STATE.cursor = 0;
    var v = trace.violation;
    $("#tape-title").textContent = "seed " + trace.seed + " · " + trace.variant +
      " · " + trace.eventCount + " events · " + trace.faultCount + " fault" + (trace.faultCount === 1 ? "" : "s");
    $("#tape-meta").textContent = (v ? "violated: " + v.name : "completed cleanly") +
      " · workload " + trace.workload + " item" + (trace.workload === 1 ? "" : "s") +
      " · digest " + trace.digest.slice(0, 16);
    $("#scrub").max = trace.events.length - 1;
    $("#btn-breach").disabled = !v;
    renderTape();
    renderDetail();
  }

  /* ── playback + keyboard ──────────────────────────────────────── */

  function stopPlay() {
    if (STATE.playing) { clearInterval(STATE.playing); STATE.playing = 0; $("#btn-play").textContent = "play"; }
  }
  function togglePlay() {
    if (STATE.playing) return stopPlay();
    if (!STATE.trace) return;
    if (STATE.cursor >= STATE.trace.events.length - 1) STATE.cursor = -1;
    $("#btn-play").textContent = "pause";
    STATE.playing = setInterval(function () {
      if (!STATE.trace || STATE.cursor >= STATE.trace.events.length - 1) return stopPlay();
      setCursor(STATE.cursor + 1);
    }, 190);
  }
  function jumpBreach() {
    if (!STATE.trace) return;
    for (var i = STATE.trace.events.length - 1; i >= 0; i--) {
      if (STATE.trace.events[i].kind === "VIOLATION") { stopPlay(); return setCursor(i); }
    }
  }

  function wireControls() {
    $("#btn-first").addEventListener("click", function () { stopPlay(); setCursor(0); });
    $("#btn-prev").addEventListener("click", function () { stopPlay(); setCursor(STATE.cursor - 1); });
    $("#btn-next").addEventListener("click", function () { stopPlay(); setCursor(STATE.cursor + 1); });
    $("#btn-last").addEventListener("click", function () { stopPlay(); setCursor(1e9); });
    $("#btn-play").addEventListener("click", togglePlay);
    $("#btn-breach").addEventListener("click", jumpBreach);
    $("#scrub").addEventListener("input", function (ev) { stopPlay(); setCursor(Number(ev.target.value)); });

    $$(".tape-axis-mode button").forEach(function (b) {
      b.addEventListener("click", function () {
        $$(".tape-axis-mode button").forEach(function (o) { o.classList.remove("on"); });
        b.classList.add("on");
        STATE.axis = b.dataset.axis;
        renderTape();
      });
    });

    var shell = $("#tape-scroll");
    shell.tabIndex = 0;
    shell.setAttribute("role", "application");
    shell.setAttribute("aria-label", "Trace timeline. Arrow keys step through events.");
    shell.addEventListener("keydown", function (ev) {
      var k = ev.key;
      if (k === "ArrowRight" || k === "ArrowDown") { stopPlay(); setCursor(STATE.cursor + 1); }
      else if (k === "ArrowLeft" || k === "ArrowUp") { stopPlay(); setCursor(STATE.cursor - 1); }
      else if (k === "Home") { stopPlay(); setCursor(0); }
      else if (k === "End") { stopPlay(); setCursor(1e9); }
      else if (k === "PageDown") { stopPlay(); setCursor(STATE.cursor + 10); }
      else if (k === "PageUp") { stopPlay(); setCursor(STATE.cursor - 10); }
      else if (k === "v" || k === "V") { jumpBreach(); }
      else if (k === " " || k === "Spacebar") { togglePlay(); }
      else return;
      ev.preventDefault();
    });
  }

  /* ── the seed plate ───────────────────────────────────────────── */

  function featuredFor(variant, seed) {
    var f = STATE.featured && STATE.featured.traces;
    if (!f) return null;
    if (variant === "buggy" && seed === 1) return f["planted-original"];
    if (variant === "claim" && seed === 11) return f["unplanted-original"];
    return null;
  }

  function loadBank(variant) {
    if (STATE.banks[variant]) return Promise.resolve(STATE.banks[variant]);
    if (STATE.bankLoading[variant]) return STATE.bankLoading[variant];
    STATE.bankLoading[variant] = fetch("data/seeds-" + variant + ".json")
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (d) { STATE.banks[variant] = d; return d; });
    return STATE.bankLoading[variant];
  }

  function fillReadout(host, pairs) {
    host.innerHTML = "";
    pairs.forEach(function (pair) {
      var d = elem("div");
      d.appendChild(elem("dt", null, pair[0]));
      d.appendChild(elem("dd", null, pair[1]));
      host.appendChild(d);
    });
  }

  function selectSeed(seed, variant) {
    STATE.seed = seed; STATE.variant = variant;
    stopPlay();
    var idx = STATE.manifest.seeds[variant];
    var row = idx.summaries[String(seed)];
    var status = $("#plate-status"), readout = $("#plate-readout");
    $("#plate-serial").textContent = variant + " · 4 items · realistic faults";

    if (!row) {
      status.innerHTML = "Seed " + num(seed) + " is outside the exported set — this page ships runs for " +
        "seeds 0–" + (idx.seedsSampled - 1) + ". <code>deterministic-testing search</code> takes any integer; the browser only " +
        "reads what was recorded. The trace below is still the last one you loaded.";
      fillReadout(readout, [["events", "—"], ["steps", "—"], ["faults injected", "—"],
        ["sim. time", "—"], ["trace digest", "not exported"], ["outcome", "unknown"]]);
      return;
    }
    var st = row[0], events = row[1], steps = row[2], faults = row[3], end = row[4], dig = row[5];
    var bad = st === "violation";
    status.innerHTML = "This seed " + (bad
      ? "<span class='bad'>violates</span> “each item credited at most once”"
      : "<span class='ok'>completes cleanly</span>") +
      ". " + (seed < idx.tracesStored
        ? "Its full trace is loaded below."
        : "Only its outcome is stored; full traces are kept for seeds 0–" + (idx.tracesStored - 1) + ".");

    fillReadout(readout, [["events", num(events)], ["steps", num(steps)], ["faults injected", num(faults)],
      ["sim. time", (end / 1e6).toFixed(3) + "s"], ["trace digest", dig], ["outcome", st]]);

    var pre = featuredFor(variant, seed);
    if (pre) return loadTrace(pre);
    if (seed >= idx.tracesStored) return;
    loadBank(variant).then(function (bank) {
      var t = bank.traces[String(seed)];
      if (t && STATE.seed === seed && STATE.variant === variant) loadTrace(t);
    })["catch"](function () {
      status.innerHTML += " <span class='bad'>Could not load the stored traces.</span>";
    });
  }

  function wirePlate() {
    var input = $("#seed-input");
    function commit() {
      var v = parseInt(String(input.value).replace(/[^0-9]/g, ""), 10);
      if (isNaN(v) || v < 0) v = 0;
      input.value = v;
      selectSeed(v, STATE.variant);
    }
    input.addEventListener("change", commit);
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { commit(); input.blur(); }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); input.value = (Number(input.value) || 0) + 1; commit(); }
      else if (ev.key === "ArrowDown") { ev.preventDefault(); input.value = Math.max(0, (Number(input.value) || 0) - 1); commit(); }
    });
    $("#seed-prev").addEventListener("click", function () { input.value = Math.max(0, (Number(input.value) || 0) - 1); commit(); });
    $("#seed-next").addEventListener("click", function () { input.value = (Number(input.value) || 0) + 1; commit(); });

    $$(".plate-variants button").forEach(function (b) {
      b.addEventListener("click", function () {
        $$(".plate-variants button").forEach(function (o) { o.classList.remove("on"); });
        b.classList.add("on");
        selectSeed(STATE.seed, b.dataset.variant);
      });
    });
  }

  /* ── the shrink collapse ──────────────────────────────────────── */

  function tickClass(e) {
    if (e.kind === "VIOLATION") return "breach";
    if (e.kind === "DROP" || e.kind === "DELAY" || e.kind === "DUPLICATE") return "fault";
    if (e.quiet) return "gone";
    return "keep";
  }

  function renderCollapse() {
    var host = $("#collapse");
    var story = STATE.story;
    var stats = STATE.manifest.shrink[story];
    var orig = STATE.featured.traces[story + "-original"];
    var min = STATE.featured.traces[story + "-shrunk"];
    if (!stats || !orig || !min) return;

    host.innerHTML = "";
    var W = Math.max(560, host.clientWidth - 40);
    var LAB = 132, PADR = 132;
    var trackW = W - LAB - PADR;
    var yA = 54, yB = 176, tickH = 26;
    var H = 236;

    var s = svg("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img" });
    s.setAttribute("aria-label",
      "Shrinking " + stats.originalEvents + " events down to " + stats.minimalEvents +
      " for seed " + stats.seed + " on the " + stats.variant + " consumer.");

    function row(y, count, name, sub, cls) {
      s.appendChild(svg("text", { x: 0, y: y - 22, "class": "row-label" }, name));
      var t = svg("text", { x: 0, y: y + 4 });
      t.appendChild(svg("tspan", { "class": "row-count" }, num(count)));
      t.appendChild(svg("tspan", { "class": "row-unit", dx: 6 }, "events"));
      s.appendChild(t);
      s.appendChild(svg("text", { x: 0, y: y + 20, "class": "row-unit" }, sub));
    }
    var minW = stats.minimalWorkload || stats.originalWorkload;
    row(yA + 4, stats.originalEvents, "as found", stats.originalFaults + " faults · " + stats.originalWorkload + " items");
    row(yB + 4, stats.minimalEvents, "after shrinking", stats.minimalFaults + " fault" + (stats.minimalFaults === 1 ? "" : "s") + " · " + minW + " item" + (minW === 1 ? "" : "s"));

    s.appendChild(svg("line", { x1: LAB, y1: yA + tickH + 6, x2: LAB + trackW, y2: yA + tickH + 6, "class": "baseline" }));
    s.appendChild(svg("line", { x1: LAB, y1: yB - 6, x2: LAB + trackW, y2: yB - 6, "class": "baseline" }));

    var nA = orig.events.length, nB = min.events.length;
    var stepA = trackW / Math.max(1, nA), stepB = trackW / Math.max(1, nB);
    var xA = function (i) { return LAB + i * stepA + stepA / 2; };
    var xB = function (j) { return LAB + j * stepB + stepB / 2; };

    var kept = {};
    stats.align.forEach(function (i) { if (i !== null && i !== undefined) kept[i] = 1; });

    var gThread = svg("g"), gA = svg("g"), gB = svg("g");

    orig.events.forEach(function (e, i) {
      var survives = kept[i];
      var cls = "tick " + (survives ? tickClass(e) : "gone") + (survives ? "" : " anim-gone");
      var line = svg("line", {
        x1: xA(i), y1: yA, x2: xA(i), y2: yA + tickH, "class": cls,
        "stroke-width": Math.max(2, Math.min(5, stepA - 1.6))
      });
      if (!survives) line.style.setProperty("--step", (i * 5) + "ms");
      gA.appendChild(line);
    });

    min.events.forEach(function (e, j) {
      var src = stats.align[j];
      var cls = "tick " + (src === null || src === undefined ? "new" : tickClass(e));
      gB.appendChild(svg("line", {
        x1: xB(j), y1: yB, x2: xB(j), y2: yB + tickH, "class": cls,
        "stroke-width": Math.max(2, Math.min(5, stepB - 1.6))
      }));
      if (src === null || src === undefined) return;
      var x0 = xA(src), y0 = yA + tickH + 6, x1 = xB(j), y1 = yB - 6;
      var d = "M" + x0 + " " + y0 + " C" + x0 + " " + (y0 + 38) + " " + x1 + " " + (y1 - 38) + " " + x1 + " " + y1;
      var p = svg("path", { d: d, "class": "thread live anim-thread" });
      var len = Math.hypot(x1 - x0, y1 - y0) + 60;
      p.setAttribute("stroke-dasharray", len);
      p.setAttribute("stroke-dashoffset", STATE.reduce ? 0 : len);
      p.style.setProperty("--step", (j * 7) + "ms");
      gThread.appendChild(p);
    });

    s.appendChild(gThread); s.appendChild(gA); s.appendChild(gB);

    // right-hand summary
    var right = LAB + trackW + 16;
    s.appendChild(svg("text", { x: right, y: yA + 16, "class": "row-unit" }, stats.candidates + " candidate"));
    s.appendChild(svg("text", { x: right, y: yA + 30, "class": "row-unit" }, "re-runs"));
    var keptN = Object.keys(kept).length;
    s.appendChild(svg("text", { x: right, y: yB + 16, "class": "row-unit" }, num(nA - keptN) + " events"));
    s.appendChild(svg("text", { x: right, y: yB + 30, "class": "row-unit" }, "removed"));

    host.appendChild(s);

    var key = elem("p", "collapse-key");
    key.innerHTML =
      "<span class='sw keep'></span>survived · <span class='sw fault'></span>injected fault · " +
      "<span class='sw breach'></span>the violation · <span class='sw new'></span>only in the shrunk run · " +
      "<span class='sw gone'></span>removed. Threads join events the two runs have in common.";
    host.appendChild(key);
    armCollapse(host);
  }

  var collapseObserver = null;
  function armCollapse(host) {
    function run() { host.classList.add("run"); }
    if (STATE.reduce) return run();
    if (!("IntersectionObserver" in window)) return run();
    if (collapseObserver) collapseObserver.disconnect();
    collapseObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { if (en.isIntersecting) { run(); collapseObserver.disconnect(); } });
    }, { threshold: 0.35 });
    collapseObserver.observe(host);
  }

  function renderShrinkNote() {
    var stats = STATE.manifest.shrink[STATE.story];
    var host = $("#shrink-note");
    host.innerHTML = "";
    host.appendChild(elem("h3", null, stats.title));

    var p = elem("p");
    if (STATE.story === "planted") {
      p.innerHTML = "Seed 1 fails with <span class='fig'>" + stats.originalEvents + " events</span> and <span class='fig'>" +
        stats.originalFaults + " injected faults</span> across " + stats.originalWorkload + " items. " +
        stats.candidates + " candidate re-runs later, the same invariant still breaks with <span class='fig'>" +
        stats.minimalEvents + " events</span>, <span class='fig'>one item</span> and <span class='fig'>one dropped message</span>. " +
        "The store's acknowledgement to <code>worker-a</code> is dropped; <code>worker-a</code> times out before it records the key; " +
        "the broker's retry lands on <code>worker-b</code>, which reads a dedup table that still says “not seen”.";
    } else {
      p.innerHTML = "The obvious repair is to make the check and the mark one atomic <code>claim</code>. It closes the window, " +
        "and it does fix seed 1. Over 10,000 seeds it still failed <span class='fig'>1,158 times</span>. Seed 11 shrinks only from <span class='fig'>" +
        stats.originalEvents + "</span> to <span class='fig'>" + stats.minimalEvents + " events</span>: the network duplicated a " +
        "<code>claim_reply(won=True)</code>, the spare copy sat in the worker's mailbox, the ack was delayed past the retry timeout, " +
        "and the redelivered job consumed the stale duplicate — because replies were correlated on <code>(op, key)</code> with no request id.";
    }
    host.appendChild(p);

    var caveat = elem("div", "caveat");
    caveat.innerHTML = STATE.story === "planted"
      ? "<strong>Shrinking finds something smaller, not something minimal.</strong> Removing a fault changes the message sequence, so this is a search over configurations, each one genuinely re-run and re-checked."
      : "<strong>The tool is much better on some inputs than others.</strong> " + stats.originalEvents + " → " + stats.minimalEvents +
        " events is a poor reduction, and the report says so rather than hiding it. The <em>bug</em> is still exactly reproducible; only the noise around it resisted removal.";
    host.appendChild(caveat);

    var open = elem("button", "cmd");
    open.type = "button";
    open.style.cursor = "pointer";
    open.style.width = "100%";
    open.style.textAlign = "left";
    open.style.marginTop = "14px";
    open.textContent = "→ open the shrunk trace in the timeline above";
    open.addEventListener("click", function () {
      loadTrace(STATE.featured.traces[STATE.story + "-shrunk"]);
      $("#plate-status").innerHTML = "Showing the <strong>shrunk</strong> scenario for seed " + stats.seed +
        " — " + stats.minimalEvents + " events, " + stats.minimalFaults + " fault" +
        (stats.minimalFaults === 1 ? "" : "s") + ", replayed from " + stats.minimalBytes + " bytes of JSON.";
      $("#trace").scrollIntoView({ behavior: STATE.reduce ? "auto" : "smooth", block: "start" });
    });
    host.appendChild(open);

    $("#minimal-json").textContent = stats.minimalText || JSON.stringify(stats.minimalScenario, null, 2);
    $("#minimal-bytes").textContent = stats.minimalBytes + " bytes on disk";
    $("#minimal-cmd").textContent = "deterministic-testing replay " + stats.seed + " --plan " + stats.artifact;
  }

  function wireShrink() {
    $$(".shrink-switch button").forEach(function (b) {
      b.addEventListener("click", function () {
        $$(".shrink-switch button").forEach(function (o) { o.classList.remove("on"); });
        b.classList.add("on");
        STATE.story = b.dataset.story;
        $("#collapse").classList.remove("run");
        renderCollapse();
        renderShrinkNote();
      });
    });
  }

  /* ── the variant matrix ───────────────────────────────────────── */

  var ROWS = [
    { key: "buggy", name: "buggy", desc: "check the dedup table, credit, then mark" },
    { key: "claim", name: "claim", desc: "one atomic claim, then credit" },
    { key: "fixed", name: "fixed", desc: "idempotent apply, replies correlated by request id" }
  ];

  function renderMatrix() {
    var host = $("#matrix-chart");
    var m = STATE.manifest.matrix;
    var total = m.seeds_per_cell;
    host.innerHTML = "";

    var W = Math.max(420, host.clientWidth - 44);
    var LAB = Math.min(210, Math.max(140, W * 0.28));
    var VAL = 108;
    var trackW = W - LAB - VAL;
    var rowH = 82, top = 34;
    var H = top + ROWS.length * rowH + 20;

    var s = svg("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img" });
    s.setAttribute("aria-label",
      "Violations out of " + num(total) + " seeds: buggy " + num(m.results["buggy/realistic"].statuses.violation) +
      ", claim " + num(m.results["claim/realistic"].statuses.violation) + ", fixed 0.");

    s.appendChild(svg("text", { x: LAB, y: 16, "class": "m-head" },
      trackW > 330 ? "violations in " + num(total) + " seeds · unreliable network" : "violations / " + num(total)));
    s.appendChild(svg("line", { x1: LAB, y1: 24, x2: LAB + trackW, y2: 24, "class": "m-track", stroke: "var(--rule)" }));

    ROWS.forEach(function (r, i) {
      var cell = m.results[r.key + "/realistic"];
      var perfect = m.results[r.key + "/none"];
      var v = cell.statuses.violation || 0;
      var y = top + i * rowH;

      s.appendChild(svg("text", { x: 0, y: y + 16, "class": "m-name" }, r.name));
      wrap(r.desc, Math.floor((LAB - 16) / 5.6)).forEach(function (line, n) {
        s.appendChild(svg("text", { x: 0, y: y + 32 + n * 13, "class": "m-desc" }, line));
      });

      s.appendChild(svg("rect", { x: LAB, y: y + 6, width: trackW, height: 22, "class": "m-track", rx: 2 }));
      var w = (v / total) * trackW;
      s.appendChild(svg("rect", {
        x: LAB, y: y + 6, width: v > 0 ? Math.max(2, w) : 3, height: 22,
        "class": "m-bar" + (v > 0 ? "" : " zero"), rx: 2
      }));

      s.appendChild(svg("text", { x: LAB + trackW + 12, y: y + 20, "class": "m-val" }, num(v)));
      s.appendChild(svg("text", { x: LAB + trackW + 12, y: y + 34, "class": "m-pct" }, pct(v, total) + " of seeds"));
      s.appendChild(svg("text", {
        x: LAB, y: y + 45, "class": "m-perfect"
      }, "perfect network: " + num((perfect.statuses.completed || 0)) + " / " + num(total) + " pass"));
    });

    host.appendChild(s);
  }

  function renderArgument() {
    var m = STATE.manifest.matrix, total = m.seeds_per_cell;
    var buggy = m.results["buggy/realistic"].statuses.violation;
    var claim = m.results["claim/realistic"].statuses.violation;
    var host = $("#matrix-argument");
    host.innerHTML = "";
    host.appendChild(elem("h3", null, "The middle row is the argument for the whole tool"));
    var p = elem("p");
    p.innerHTML = "The atomic <code>claim</code> takes the failure rate from " + pct(buggy, total) +
      " to " + pct(claim, total) + ". Nothing about that looks like a bug that is still there.";
    host.appendChild(p);
    var pull = elem("div", "pull");
    pull.textContent = "In production the bug moves from “happens most days” to “happens about weekly” — which is exactly the regime where people stop investigating and add a reconciliation job instead.";
    host.appendChild(pull);
    var p2 = elem("p");
    p2.innerHTML = "The remaining failure was not planted. It was found, shrunk and read: replies correlated on <code>(op, key)</code> " +
      "cannot tell this request's answer from a duplicated answer to the last one. The final version adds a per-request id and " +
      "survives " + num(total) + " seeds.";
    host.appendChild(p2);
  }

  /* ── gauges + proof ───────────────────────────────────────────── */

  function renderGauges() {
    var m = STATE.manifest, mx = m.matrix, total = mx.seeds_per_cell;
    var buggy = mx.results["buggy/realistic"].statuses.violation;
    var claim = mx.results["claim/realistic"].statuses.violation;
    var pl = m.shrink.planted;
    var host = $("#gauges");
    host.innerHTML = "";
    [
      { cls: "warn", val: num(buggy), unit: "/ " + num(total), lab: "seeds where the pipeline credits an item twice" },
      { cls: "warn", val: num(claim), unit: "/ " + num(total), lab: "still failing after the fix that looked like a fix" },
      { cls: "", val: pl.originalEvents + " → " + pl.minimalEvents, unit: "events", lab: "seed 1 shrunk to one item and one dropped message" },
      { cls: "good", val: m.determinism.distinctDigests, unit: "digest", lab: num(m.determinism.runs) + " replays of one seed, one SHA-256" }
    ].forEach(function (g) {
      var li = elem("li", g.cls);
      var v = elem("span", "g-val");
      v.appendChild(document.createTextNode(g.val + " "));
      var u = elem("span", "unit", g.unit);
      v.appendChild(u);
      li.appendChild(v);
      li.appendChild(elem("span", "g-lab", g.lab));
      host.appendChild(li);
    });
  }

  function renderProof() {
    var m = STATE.manifest;
    var tp = m.throughput.measurements.fixed;
    var host = $("#proof-grid");
    host.innerHTML = "";
    var cards = [
      {
        h: "determinism",
        v: m.determinism.distinctDigests, u: "distinct digest",
        p: "Seed 1 was replayed " + num(m.determinism.runs) + " times while this page's data was exported, in " +
           m.determinism.seconds + "s. Every run produced the same SHA-256 over the full event log: <code>" +
           m.determinism.digest.slice(0, 24) + "…</code>"
      },
      {
        h: "the test that could not fail",
        v: "5 / 5", u: "parameters caught",
        p: "The 100-run digest test passed against a deliberately planted <code>set</code>-iteration mutant. Only the cross-process test, which shells out to interpreters with different <code>PYTHONHASHSEED</code> values, failed it. A test that cannot fail is not evidence."
      },
      {
        h: "throughput",
        v: num(tp.seeds), u: "seeds in " + tp.wall_seconds + "s",
        p: "Covering " + tp.simulated_hours + " hours of simulated time on one core, a speedup of about " +
           num(tp.speedup_vs_wallclock) + "×. The virtual clock jumps straight to the next scheduled instant, so a 30-second idle timeout is free."
      },
      {
        h: "test suite",
        v: "98", u: "passing",
        p: "Determinism in and across processes, virtual-clock exactness, deadlock and step-limit detection, fault-plan round-tripping, <code>ddmin</code> correctness, CLI exit codes, and the fixed pipeline surviving 2,000 seeds."
      }
    ];
    cards.forEach(function (c) {
      var a = elem("article");
      a.appendChild(elem("h3", null, c.h));
      var v = elem("span", "p-val");
      v.appendChild(document.createTextNode(c.v + " "));
      v.appendChild(elem("span", "unit", c.u));
      a.appendChild(v);
      var p = elem("p"); p.innerHTML = c.p; a.appendChild(p);
      host.appendChild(a);
    });
  }

  /* ── boot ─────────────────────────────────────────────────────── */

  function redrawAll() {
    if (!STATE.manifest) return;
    renderTape();
    renderCollapse();
    renderMatrix();
  }

  var resizeTimer = 0;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(redrawAll, 140);
  });

  function fail(err) {
    var host = $(".hero");
    var box = elem("div", "dataerr");
    box.innerHTML = "<strong>The recorded runs did not load.</strong> This page reads JSON from " +
      "<code>web/data/</code> over HTTP, so it needs a server rather than a <code>file://</code> URL. " +
      "Generate the data and serve it:<br><br><code>python scripts/export_web_data.py</code><br>" +
      "<code>cd web &amp;&amp; python -m http.server 8000</code>" +
      (err ? "<br><br><code>" + String(err) + "</code>" : "");
    host.appendChild(box);
  }

  Promise.all([
    fetch("data/manifest.json").then(function (r) { if (!r.ok) throw new Error("manifest.json: HTTP " + r.status); return r.json(); }),
    fetch("data/featured.json").then(function (r) { if (!r.ok) throw new Error("featured.json: HTTP " + r.status); return r.json(); })
  ]).then(function (both) {
    STATE.manifest = both[0];
    STATE.featured = both[1];

    renderGauges();
    renderProof();
    renderMatrix();
    renderArgument();
    wireControls();
    wirePlate();
    wireShrink();
    renderShrinkNote();
    renderCollapse();
    selectSeed(1, "buggy");

    $("#stamp").textContent = "exported " + STATE.manifest.generated.replace("T", " ") +
      " · python " + STATE.manifest.python;
  })["catch"](fail);

})();
