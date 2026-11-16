/*
 * Telemetry recorder (Phase 6) — capture only, no judgment.
 *
 * Captures behavioral signals in the student's browser and ships them to the
 * append-only ingest endpoint in batches. It records; it never decides anything
 * (golden rule #6): the server stamps the trusted timestamp and later layers
 * (integrity features, LLM verdict) interpret the stream.
 *
 * Captured events (see contracts EventType):
 *   visibility_hidden / visibility_visible  - tab backgrounded / returned (+duration)
 *   window_blur / window_focus              - window lost / gained focus
 *   pagehide                                - page being unloaded
 *   interaction                             - per-question pointer/key activity
 *   answer_change                           - a question's answer was changed
 *   audio_play / audio_seek                 - listening audio transport
 *   fullscreen_exit                         - the exam left fullscreen
 *   paste_attempt                           - a BLOCKED paste/drop: LENGTH only
 *   field_focus / field_blur                - a tracked writing field gained/lost focus
 *   writing_stats                           - aggregated typing stats per writing field
 *   input_burst                             - one input that added >= 2 chars at once
 *
 * Writing fields (`trackWriting`) are measured WITHOUT storing content: no key
 * identities (only a keydown count + inter-key timing), no clipboard text (only
 * the attempted length), no inserted text (only the raw InputEvent.inputType and
 * how many chars it added). Stats are additive deltas per window, flushed on blur,
 * every WRITING_FLUSH_MS, on hide/pagehide/stop and on `flushWriting()`. The
 * server validates these payloads with `extra="forbid"` (app/telemetry/schema.py).
 *
 * Usage:
 *   const rec = ExamRecorder.start({ attemptId, endpoint });
 *   rec.setItem('q-3');                 // focus a question
 *   rec.answerChange('q-3', 'B');       // record an answer change
 *   rec.attachAudio(audioEl, 'sec-1');  // wire audio_play / audio_seek
 *   rec.trackWriting(textarea, 'w-1', 'open_writing');  // typing stats
 *   rec.pasteAttempt('w-1', 'paste', 312, 'open_writing');  // blocked paste
 *   rec.flushWriting('submit'); await rec.drain(3000);   // before submit
 *
 * Transport (Phase 13 reliability rework):
 *   - Events stay in the queue until the server acknowledged them (2xx). A failed
 *     send puts them back at the FRONT of the queue and the next tick retries with
 *     exponential backoff (1 s, 2 s, 4 s … capped at 30 s). A fetch with no
 *     response after FETCH_TIMEOUT_MS (15 s) is aborted and counts as a failure.
 *   - Batches are split so each request stays under ~50 KB (sendBeacon /
 *     keepalive fetch cap is ~64 KB).
 *   - A batch the server rejects with 422 (one event failed the typed payload
 *     validation) is NOT dropped whole: it is bisected and re-sent, so only the
 *     event(s) the server keeps rejecting are dropped (console warning) and the
 *     good ones are delivered. Any other permanent 4xx still drops the batch.
 *   - The in-memory queue is capped (MAX_QUEUE); when it overflows the oldest
 *     events are dropped with a console warning.
 *   - The unsent queue is persisted to localStorage (bounded) whenever a send
 *     fails and on pagehide unless accepted beacons took all of it, and resent on the
 *     next load for the same attempt. Restored events stay in the snapshot until
 *     a 2xx drains the queue, so a load that dies before its first successful
 *     send still hands them on to the next one.
 *   - `navigator.sendBeacon` is used on pagehide only, and only on a healthy
 *     network (the page is dying and we cannot observe a result); an accepted
 *     beacon then clears the snapshot (no replay). While the network is
 *     known-bad (a send failed, or restored events have not had a 2xx yet) a
 *     beacon's "accepted" proves nothing, so pagehide keeps the snapshot and
 *     tries a single keepalive fetch instead. Visibility-hidden always uses a
 *     keepalive fetch so a failure is observable and retried.
 *   - Replayed events may duplicate; integrity counts are capture-only. A
 *     keepalive fetch that was in flight when the page died and still landed is
 *     resent from the snapshot on the next load (at most one chunk per load).
 *     There is no server-side dedupe (no client event ids yet), so a duplicate
 *     is tolerated over a lost event and the integrity layer's counts are
 *     advisory.
 *   - `answer_change` payloads for long text (> TEXT_FULL_MAX chars) carry only
 *     `length` + a 200-char `head` / `tail` and `truncated: true` so a single
 *     writing answer can never blow the batch size.
 */
(function (global) {
  "use strict";

  var MAX_QUEUE = 2000; // in-memory cap; oldest events are dropped beyond this
  var MAX_BATCH_BYTES = 50 * 1024; // keep every request well under the ~64 KB cap
  var MAX_PERSIST_EVENTS = 500; // bounded localStorage snapshot of the unsent queue
  var MAX_PERSIST_BYTES = 200 * 1024;
  var MAX_BACKOFF_MS = 30000;
  var FETCH_TIMEOUT_MS = 15000; // a hung send is aborted and retried like a failure
  var TEXT_FULL_MAX = 2048; // answer_change text longer than this is truncated
  var TEXT_EDGE = 200; // head/tail chars kept for a truncated answer_change
  var STORAGE_PREFIX = "exam:telemetry:";
  var WRITING_FLUSH_MS = 10000; // writing_stats window
  var BURST_MIN_CHARS = 2; // one input adding >= this many chars -> input_burst
  var PAUSE_MS = 2000; // inter-key gap above this is a pause (not active typing)
  var LONG_PAUSE_MS = 10000;
  var MAX_IK_SAMPLES = 2000; // per-window samples kept for the p50/p90 summary
  // Inter-key-interval histogram upper edges (ms). MUST match
  // app/telemetry/schema.py IK_BUCKET_EDGES_MS and app/integrity/writing.py.
  var IK_EDGES = [50, 100, 150, 200, 300, 500, 750, 1000, 2000, 10000];
  var MODIFIER_KEYS = { Shift: 1, Control: 1, Alt: 1, Meta: 1, CapsLock: 1, AltGraph: 1 };
  // Server limits for the writing_stats maps (app/telemetry/schema.py): an
  // inputType key is at most 48 chars and a map holds at most 32 of them.
  var INPUT_TYPE_MAX = 48;
  var MAX_INPUT_TYPES = 32;

  function nowIso() {
    return new Date().toISOString();
  }

  function byteLength(s) {
    if (global.TextEncoder) {
      try {
        return new TextEncoder().encode(s).length;
      } catch (e) {
        /* fall through */
      }
    }
    return s.length;
  }

  function warn() {
    if (global.console && console.warn) {
      console.warn.apply(console, ["[recorder]"].concat(Array.prototype.slice.call(arguments)));
    }
  }

  function monoNow() {
    return global.performance && performance.now ? performance.now() : Date.now();
  }

  function percentile(sorted, q) {
    if (!sorted.length) return null;
    var pos = (sorted.length - 1) * q;
    var lo = Math.floor(pos);
    var hi = Math.ceil(pos);
    return Math.round(sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo));
  }

  // -- writing-field tracker (shapes only, never content) -------------------- //
  function WritingTracker(rec, el, itemId, field) {
    this.rec = rec;
    this.el = el;
    this.itemId = itemId;
    this.field = field || "open_writing";
    this.prevLen = (el.value || "").length;
    this.lastKeyAt = null;
    this.pendingType = null;
    this._reset();
    var self = this;
    this._h = {
      focus: function () {
        rec.setItem(itemId);
        self.lastKeyAt = null; // the first key after (re)focus has no interval
        self.prevLen = (el.value || "").length;
        rec.record("field_focus", {
          item_id: itemId,
          payload: { field: self.field, length: self.prevLen },
        });
      },
      blur: function () {
        self.flush("blur");
        rec.record("field_blur", {
          item_id: itemId,
          payload: { field: self.field, length: (el.value || "").length },
        });
      },
      keydown: function (e) {
        if (e.key && MODIFIER_KEYS[e.key]) return; // count keys, never identify them
        var now = monoNow();
        self.keystrokes += 1;
        if (self.lastKeyAt !== null) self._interval(now - self.lastKeyAt);
        self.lastKeyAt = now;
        self.dirty = true;
      },
      beforeinput: function (e) {
        self.pendingType = e.inputType || "";
      },
      input: function (e) {
        // Same 48-char cap as the burst payload: a longer key (a script can
        // dispatch any inputType) would make the server reject the whole
        // writing_stats event. Beyond 31 distinct types the rest fold into "other".
        var type = String(e.inputType || self.pendingType || "").slice(0, INPUT_TYPE_MAX);
        if (!(type in self.added) && Object.keys(self.added).length >= MAX_INPUT_TYPES - 1) {
          type = "other";
        }
        self.pendingType = null;
        var trusted = e.isTrusted !== false;
        if (!trusted) self.untrusted += 1;
        var len = (el.value || "").length;
        var delta = len - self.prevLen;
        self.prevLen = len;
        if (delta > 0) {
          self.added[type] = (self.added[type] || 0) + delta;
          self.inserts[type] = (self.inserts[type] || 0) + 1;
          self.maxInsert[type] = Math.max(self.maxInsert[type] || 0, delta);
          if (delta >= BURST_MIN_CHARS) {
            rec.record("input_burst", {
              item_id: itemId,
              payload: {
                field: self.field,
                input_type: type.slice(0, INPUT_TYPE_MAX),
                length: delta,
                field_length: len,
                trusted: trusted,
              },
            });
          }
        } else if (delta < 0) {
          self.deleted += -delta;
        }
        self.dirty = true;
      },
    };
    for (var k in this._h) el.addEventListener(k, this._h[k]);
  }

  WritingTracker.prototype._reset = function () {
    this.windowStart = Date.now();
    // Prototype-less maps: a script-dispatched inputType such as "constructor" or
    // "__proto__" must be an ordinary key, not a lookup on Object.prototype.
    this.added = Object.create(null);
    this.inserts = Object.create(null);
    this.maxInsert = Object.create(null);
    this.deleted = 0;
    this.keystrokes = 0;
    this.untrusted = 0;
    this.ikCount = 0;
    this.ikSum = 0;
    this.ikSamples = [];
    this.hist = IK_EDGES.map(function () {
      return 0;
    }).concat([0]);
    this.pauses2 = 0;
    this.pauses10 = 0;
    this.active = 0;
    this.dirty = false;
  };

  WritingTracker.prototype._interval = function (iv) {
    iv = Math.max(0, iv);
    this.ikCount += 1;
    this.ikSum += iv;
    if (this.ikSamples.length < MAX_IK_SAMPLES) this.ikSamples.push(iv);
    var b = 0;
    while (b < IK_EDGES.length && iv >= IK_EDGES[b]) b += 1;
    this.hist[b] += 1;
    if (iv > PAUSE_MS) this.pauses2 += 1;
    else this.active += iv;
    if (iv > LONG_PAUSE_MS) this.pauses10 += 1;
  };

  // Emit this window's aggregates as one writing_stats event (if anything happened).
  WritingTracker.prototype.flush = function (reason) {
    if (!this.dirty) return false;
    var sorted = this.ikSamples.slice().sort(function (a, b) {
      return a - b;
    });
    this.rec.record("writing_stats", {
      item_id: this.itemId,
      payload: {
        field: this.field,
        reason: String(reason || "interval").slice(0, 32),
        window_ms: Math.max(0, Date.now() - this.windowStart),
        length: (this.el.value || "").length,
        added: this.added,
        inserts: this.inserts,
        max_insert: this.maxInsert,
        deleted_chars: this.deleted,
        keystrokes: this.keystrokes,
        untrusted_inputs: this.untrusted,
        ik_count: this.ikCount,
        ik_sum_ms: Math.round(this.ikSum),
        ik_mean_ms: this.ikCount ? Math.round(this.ikSum / this.ikCount) : null,
        ik_p50_ms: percentile(sorted, 0.5),
        ik_p90_ms: percentile(sorted, 0.9),
        pauses_2s: this.pauses2,
        pauses_10s: this.pauses10,
        ik_hist: this.hist,
        active_ms: Math.round(this.active),
      },
    });
    this._reset();
    return true;
  };

  WritingTracker.prototype.detach = function () {
    for (var k in this._h) this.el.removeEventListener(k, this._h[k]);
  };

  function Recorder(opts) {
    this.attemptId = opts.attemptId;
    // Endpoint may be templated with the attempt id, or a plain base path.
    this.endpoint =
      opts.endpoint || "/attempts/" + encodeURIComponent(this.attemptId) + "/events";
    this.flushIntervalMs = opts.flushIntervalMs || 5000;
    this.fetchTimeoutMs = opts.fetchTimeoutMs || FETCH_TIMEOUT_MS;
    this.storageKey = STORAGE_PREFIX + this.attemptId;
    this.queue = [];
    this.currentItemId = null;
    this._hiddenSince = null; // ms timestamp when the tab went hidden
    this._timer = null;
    this._bound = {};
    this._sending = false; // one fetch in flight at a time (ordering + no dupes)
    this._inflight = []; // the chunk currently being fetched (not in `queue`)
    this._failures = 0; // consecutive failed sends (drives the backoff)
    this._retryAt = 0; // ms epoch before which a non-urgent flush is skipped
    this._dropped = 0; // events dropped because the queue overflowed
    // True from a restore until the first 2xx: the network was bad on the previous
    // load, so the chunk in flight is treated as at-risk (kept in the snapshot).
    this._restoredUnacked = false;
    // True while a 422-rejected batch is being bisected and re-sent (`_resendSplit`):
    // the not-yet-resent parts live in `_inflight` and must survive a page death.
    this._splitting = false;
    this._writing = []; // WritingTracker per tracked field (re-rendered fields re-attach)
    this._writingTimer = null;
    this._wasFullscreen = false;
  }

  Recorder.prototype.record = function (type, extra) {
    extra = extra || {};
    var event = {
      type: type,
      client_ts: nowIso(),
      item_id: extra.item_id !== undefined ? extra.item_id : this.currentItemId,
      duration_ms: extra.duration_ms !== undefined ? extra.duration_ms : null,
      payload: extra.payload || {},
    };
    this.queue.push(event);
    if (this.queue.length > MAX_QUEUE) {
      var overflow = this.queue.length - MAX_QUEUE;
      this.queue.splice(0, overflow);
      this._dropped += overflow;
      // One warning per 100 dropped events so a dead network doesn't spam the console.
      if (this._dropped === overflow || this._dropped % 100 === 0) {
        warn("telemetry queue full (" + MAX_QUEUE + "); dropped " + this._dropped + " oldest events");
      }
    }
    return event;
  };

  // -- public capture helpers ------------------------------------------------ //
  Recorder.prototype.setItem = function (itemId) {
    this.currentItemId = itemId;
  };

  Recorder.prototype.interaction = function (itemId, payload) {
    this.record("interaction", { item_id: itemId, payload: payload });
  };

  // The raw new value is captured on purpose (see telemetry CLAUDE.md). Long
  // text (open_writing) is truncated so one answer never dominates a batch:
  //   { value: <head 200 chars>, tail: <last 200 chars>, length: N, truncated: true }
  Recorder.prototype.answerChange = function (itemId, value) {
    var payload;
    if (typeof value === "string" && value.length > TEXT_FULL_MAX) {
      payload = {
        value: value.slice(0, TEXT_EDGE),
        tail: value.slice(-TEXT_EDGE),
        length: value.length,
        truncated: true,
      };
    } else {
      payload = { value: value };
    }
    this.record("answer_change", { item_id: itemId, payload: payload });
  };

  // Capture the browser/device fingerprint (legally observable client signals).
  // Emitted once at (re)start; the server stamps the IP on ingest. A resume on a
  // different machine produces a second, differing fingerprint that the integrity
  // layer flags — capture only, no judgment here (golden rule #6).
  Recorder.prototype.deviceInfo = function () {
    var n = global.navigator || {};
    var s = global.screen || {};
    var tz = "";
    try {
      tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    } catch (e) {
      /* Intl may be unavailable */
    }
    this.record("device_info", {
      payload: {
        user_agent: n.userAgent || "",
        platform: n.platform || "",
        language: n.language || "",
        languages: n.languages ? Array.prototype.slice.call(n.languages) : [],
        timezone: tz,
        screen: {
          width: s.width || 0,
          height: s.height || 0,
          pixel_ratio: global.devicePixelRatio || 1,
        },
        viewport: { width: global.innerWidth || 0, height: global.innerHeight || 0 },
        hardware_concurrency: n.hardwareConcurrency || null,
        device_memory: n.deviceMemory || null,
        touch: "ontouchstart" in global || (n.maxTouchPoints || 0) > 0,
      },
    });
    this.flush();
  };

  // Measure a writing field (open_writing textarea). Re-attaching the same element
  // is a no-op; trackers whose element left the DOM are flushed and dropped.
  Recorder.prototype.trackWriting = function (el, itemId, field) {
    for (var i = 0; i < this._writing.length; i++) {
      if (this._writing[i].el === el) return this._writing[i];
    }
    var t = new WritingTracker(this, el, itemId, field);
    this._writing.push(t);
    return t;
  };

  // Flush every tracked field's pending window (blur/interval/hide/submit...).
  Recorder.prototype.flushWriting = function (reason) {
    var keep = [];
    var any = false;
    for (var i = 0; i < this._writing.length; i++) {
      var t = this._writing[i];
      if (t.flush(reason)) any = true;
      if (t.el.isConnected === false) t.detach();
      else keep.push(t);
    }
    this._writing = keep;
    return any;
  };

  // A BLOCKED paste/drop. Only the LENGTH of the attempted text is recorded —
  // never the text itself (the server rejects any other payload key).
  Recorder.prototype.pasteAttempt = function (itemId, kind, length, field) {
    var payload = { kind: kind === "drop" ? "drop" : "paste", length: Math.max(0, length | 0) };
    if (field) payload.field = field;
    this.record("paste_attempt", { item_id: itemId, payload: payload });
    this.flush(false);
  };

  // Resolve once everything queued so far was acknowledged (2xx), or after
  // `maxMs` (false). Used right before submit so the integrity layer sees the
  // last writing window when the server finalizes the attempt.
  Recorder.prototype.drain = function (maxMs) {
    var self = this;
    var deadline = Date.now() + (maxMs || 3000);
    return new Promise(function (resolve) {
      (function tick() {
        if (self.queue.length === 0 && !self._sending) return resolve(true);
        if (Date.now() >= deadline) return resolve(false);
        if (!self._sending) {
          self._retryAt = 0; // a submit is worth an immediate retry
          self.flush(false);
        }
        global.setTimeout(tick, 100);
      })();
    });
  };

  Recorder.prototype.attachAudio = function (audioEl, itemId) {
    var self = this;
    audioEl.addEventListener("play", function () {
      self.record("audio_play", {
        item_id: itemId,
        payload: { position: audioEl.currentTime },
      });
    });
    audioEl.addEventListener("seeked", function () {
      self.record("audio_seek", {
        item_id: itemId,
        payload: { position: audioEl.currentTime },
      });
      self.flush();
    });
  };

  // -- persistence of the unsent queue (bounded) ----------------------------- //
  // The network is "known-bad" after a failed send, or right after a restore
  // until the first 2xx (the previous load could not deliver).
  Recorder.prototype._atRisk = function () {
    return this._failures > 0 || this._restoredUnacked;
  };

  Recorder.prototype._persist = function () {
    var store = global.localStorage;
    if (!store) return;
    // While the network is known-bad, the chunk in flight is likely to fail too,
    // so snapshot it as well (if the page dies first it would otherwise be lost).
    // On a healthy network a keepalive fetch in flight is trusted to land —
    // snapshotting it would mean a duplicate on the next load.
    // During a 422 split the "in flight" list is mostly NOT on the wire yet, so it
    // is snapshotted too.
    var all = (this._atRisk() || this._splitting ? this._inflight : []).concat(this.queue);
    try {
      if (all.length === 0) {
        store.removeItem(this.storageKey);
        return;
      }
      // Keep the NEWEST events if the snapshot has to be bounded.
      var events = all.slice(-MAX_PERSIST_EVENTS);
      var body = JSON.stringify(events);
      while (events.length > 1 && byteLength(body) > MAX_PERSIST_BYTES) {
        events = events.slice(Math.ceil(events.length / 2));
        body = JSON.stringify(events);
      }
      store.setItem(this.storageKey, body);
    } catch (e) {
      /* storage may be unavailable (private mode / quota) — best effort only */
    }
  };

  // Drop the snapshot outright (the browser accepted a beacon for everything).
  Recorder.prototype._clearSnapshot = function () {
    try {
      if (global.localStorage) localStorage.removeItem(this.storageKey);
    } catch (e) {
      /* ignore */
    }
  };

  // The snapshot is NOT removed here: the restored events are re-persisted at
  // once (as the head of the queue) and the key only goes away after a 2xx drains
  // the queue, so a page that dies before its first successful flush keeps them.
  Recorder.prototype._restore = function () {
    var store = global.localStorage;
    if (!store) return 0;
    var raw = null;
    try {
      raw = store.getItem(this.storageKey);
    } catch (e) {
      return 0;
    }
    if (!raw) return 0;
    var events;
    try {
      events = JSON.parse(raw);
    } catch (e) {
      this._clearSnapshot(); // corrupt — nothing to resend
      return 0;
    }
    if (!Array.isArray(events) || events.length === 0) {
      this._clearSnapshot();
      return 0;
    }
    this.queue = events.concat(this.queue);
    this._restoredUnacked = true;
    this._persist();
    warn("restored " + events.length + " unsent telemetry events from the previous load");
    return events.length;
  };

  // -- transport ------------------------------------------------------------- //
  // Take events off the FRONT of the queue until the batch would exceed the size
  // cap (always at least one event). Returns the chunk; the queue is left as is.
  Recorder.prototype._nextChunk = function () {
    var chunk = [];
    var size = 2; // "[]"
    for (var i = 0; i < this.queue.length; i++) {
      var s = byteLength(JSON.stringify(this.queue[i])) + 1;
      if (chunk.length > 0 && size + s > MAX_BATCH_BYTES) break;
      chunk.push(this.queue[i]);
      size += s;
    }
    return chunk;
  };

  Recorder.prototype._backoff = function () {
    this._failures += 1;
    var delay = Math.min(MAX_BACKOFF_MS, 1000 * Math.pow(2, this._failures - 1));
    this._retryAt = Date.now() + delay;
    this._persist();
    warn(
      "telemetry send failed (" + this._failures + "x); " +
        this.queue.length + " events queued, retrying in " + delay + " ms"
    );
  };

  // `urgent` = the page may be dying (pagehide): sendBeacon on a healthy network,
  // else snapshot + one keepalive fetch; ignores the backoff either way.
  // Otherwise: keepalive fetch, one request in flight at a time, honours backoff.
  Recorder.prototype.flush = function (urgent) {
    if (this.queue.length === 0) return;

    if (urgent) {
      if (!this._atRisk()) {
        // Healthy network: beacon every chunk. A beacon is fire-and-forget; once
        // the browser accepted it (true) we assume delivery, drop the events AND
        // clear the snapshot (otherwise the next load would replay them =
        // duplicates). Whatever the beacon did NOT accept (quota, no API) is
        // persisted below and handed to a keepalive fetch.
        if (global.navigator && navigator.sendBeacon) {
          while (this.queue.length > 0) {
            var chunk = this._nextChunk();
            var blob = new Blob([JSON.stringify({ events: chunk })], { type: "application/json" });
            var ok = false;
            try {
              ok = navigator.sendBeacon(this.endpoint, blob);
            } catch (e) {
              ok = false;
            }
            if (!ok) break; // rejected (e.g. quota) — fall through to fetch for the rest
            this.queue.splice(0, chunk.length);
          }
        }
        if (this.queue.length === 0) {
          // A 422 split still owns unsent events (`_inflight`): keep them
          // snapshotted for the next load instead of clearing the key.
          if (this._splitting) this._persist();
          else this._clearSnapshot();
          return;
        }
      }
      // Known-bad network (`_atRisk`: a send failed, or restored events have not
      // been acknowledged yet): no beacon. Its `true` only means "queued", Chrome
      // never retries a failed one, and trusting it would drop the snapshot =
      // lost events. Instead everything stays snapshotted and at most ONE
      // keepalive fetch is tried below (its chunk stays in the snapshot as the
      // in-flight chunk), so a duplicate on the next load is bounded to that
      // chunk. Also reached when the beacon was (partly) not accepted.
      this._persist();
    }

    if (!global.fetch) return;
    if (this._sending) return;
    if (!urgent && Date.now() < this._retryAt) return;

    var self = this;
    var batch = this._nextChunk();
    this.queue.splice(0, batch.length);
    this._inflight = batch;
    this._sending = true;
    // A request that never answers (no response, no network error) would leave
    // `_sending` true for the rest of the page's life and wedge the queue. Abort it
    // after `fetchTimeoutMs` (default FETCH_TIMEOUT_MS): the fetch promise then rejects with an AbortError and
    // the `.catch` below treats it like any other failure (re-queue at the front +
    // backoff). If the server did get the batch, the retry may duplicate it — the
    // same tolerated-duplicate trade-off as the snapshot replay above.
    var controller = global.AbortController ? new AbortController() : null;
    var abortTimer = controller
      ? global.setTimeout(function () {
          warn("telemetry send timed out after " + self.fetchTimeoutMs + " ms; aborting");
          controller.abort();
        }, this.fetchTimeoutMs)
      : null;
    var settle = function () {
      if (abortTimer !== null) global.clearTimeout(abortTimer);
      self._sending = false;
      self._inflight = [];
    };
    var requeue = function () {
      // Put the failed chunk back at the FRONT so order is preserved.
      self.queue = batch.concat(self.queue);
    };
    var init = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ events: batch }),
      keepalive: true,
    };
    if (controller) init.signal = controller.signal;
    fetch(this.endpoint, init)
      .then(function (resp) {
        settle();
        if (resp.ok) {
          self._failures = 0;
          self._retryAt = 0;
          self._restoredUnacked = false;
          self._persist(); // drops the storage key once the queue is drained
          if (self.queue.length > 0) self.flush(false); // drain the next chunk
          return;
        }
        if (resp.status === 422 && batch.length > 1) {
          // Validation failed for (at least) one event. Dropping the whole batch
          // would throw away the good events next to it: bisect and re-send, and
          // drop only what the server keeps rejecting.
          self._resendSplit(batch);
          return;
        }
        if (resp.status >= 400 && resp.status < 500 && resp.status !== 408 && resp.status !== 429) {
          // The server rejected the batch itself (schema/validation) — a retry
          // can't fix that, so drop it rather than wedge the queue forever.
          warn("telemetry batch rejected with HTTP " + resp.status + "; dropped " + batch.length + " events");
          self._restoredUnacked = false; // the server answered: the network is fine
          self._persist();
          return;
        }
        requeue();
        self._backoff();
      })
      .catch(function () {
        settle();
        requeue();
        self._backoff();
      });
  };

  // One POST of `events` with the same abort-on-timeout as `flush`. Resolves with
  // the Response, rejects on a network error / timeout.
  Recorder.prototype._post = function (events) {
    var self = this;
    var controller = global.AbortController ? new AbortController() : null;
    var timer = controller
      ? global.setTimeout(function () {
          warn("telemetry send timed out after " + self.fetchTimeoutMs + " ms; aborting");
          controller.abort();
        }, this.fetchTimeoutMs)
      : null;
    var init = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ events: events }),
      keepalive: true,
    };
    if (controller) init.signal = controller.signal;
    var clear = function () {
      if (timer !== null) global.clearTimeout(timer);
    };
    return fetch(this.endpoint, init).then(
      function (resp) {
        clear();
        return resp;
      },
      function (err) {
        clear();
        throw err;
      }
    );
  };

  // A batch came back 422: at least one event in it fails the server's payload
  // validation. Bisect it — send each half on its own, split a half that is
  // rejected again — until the offending event(s) are isolated; those are dropped
  // with a console warning and every other event is delivered, in order. Each
  // event is re-sent at most ~log2(n) times. Holds `_sending` for the whole run
  // (one request in flight at a time). A transient failure mid-way (network,
  // timeout, 408/429/5xx) re-queues everything not yet delivered at the FRONT of
  // the queue and backs off like any failed send.
  Recorder.prototype._resendSplit = function (batch) {
    var self = this;
    var halve = function (events) {
      var mid = Math.ceil(events.length / 2);
      return [events.slice(0, mid), events.slice(mid)];
    };
    var flatten = function (parts) {
      return Array.prototype.concat.apply([], parts);
    };
    var work = halve(batch); // parts still to send, in order
    var dropped = 0;
    this._sending = true;
    this._splitting = true;
    this._inflight = batch.slice();

    var finish = function (transient) {
      var rest = flatten(work);
      self._sending = false;
      self._splitting = false;
      self._inflight = [];
      if (dropped > 0) {
        warn(
          "telemetry batch partly rejected (HTTP 422): dropped " + dropped + " invalid of " +
            batch.length + " events, delivered the rest"
        );
      }
      if (transient) {
        self.queue = rest.concat(self.queue);
        self._backoff();
        return;
      }
      self._failures = 0;
      self._retryAt = 0;
      self._restoredUnacked = false; // the server answered: the network is fine
      self._persist();
      if (self.queue.length > 0) self.flush(false);
    };

    var step = function () {
      if (work.length === 0) return finish(false);
      var part = work[0];
      self._inflight = flatten(work);
      self._post(part)
        .then(function (resp) {
          if (resp.ok) {
            work.shift();
            return step();
          }
          if (resp.status === 422) {
            work.shift();
            if (part.length === 1) {
              dropped += 1;
              warn("telemetry event rejected with HTTP 422; dropped 1 '" + part[0].type + "' event");
            } else {
              work = halve(part).concat(work);
            }
            return step();
          }
          if (resp.status >= 400 && resp.status < 500 && resp.status !== 408 && resp.status !== 429) {
            // Some other permanent rejection of this part: a retry can't fix it.
            work.shift();
            dropped += part.length;
            warn("telemetry batch rejected with HTTP " + resp.status + "; dropped " + part.length + " events");
            return step();
          }
          finish(true);
        })
        .catch(function () {
          finish(true);
        });
    };
    step();
  };

  // -- lifecycle ------------------------------------------------------------- //
  Recorder.prototype.start = function () {
    var self = this;

    this._bound.visibility = function () {
      if (document.visibilityState === "hidden") {
        self._hiddenSince = Date.now();
        self.flushWriting("hidden");
        self.record("visibility_hidden", {});
        // The tab may not come back — ship immediately. A keepalive fetch
        // survives the tab being backgrounded and (unlike a beacon) tells us
        // whether it arrived, so a failure is retried / persisted.
        self.flush(false);
        self._persist();
      } else {
        var duration =
          self._hiddenSince != null ? Date.now() - self._hiddenSince : null;
        self._hiddenSince = null;
        self.record("visibility_visible", { duration_ms: duration });
      }
    };
    this._bound.blur = function () {
      self.record("window_blur", {});
    };
    this._bound.focus = function () {
      self.record("window_focus", {});
    };
    this._bound.pagehide = function () {
      self.flushWriting("pagehide");
      self.record("pagehide", {});
      self.flush(true);
    };
    this._bound.fullscreen = function () {
      var fs = document.fullscreenElement || document.webkitFullscreenElement;
      if (fs) {
        self._wasFullscreen = true;
      } else if (self._wasFullscreen) {
        self._wasFullscreen = false;
        self.record("fullscreen_exit", {});
        self.flush(false);
      }
    };

    document.addEventListener("visibilitychange", this._bound.visibility);
    global.addEventListener("blur", this._bound.blur);
    global.addEventListener("focus", this._bound.focus);
    global.addEventListener("pagehide", this._bound.pagehide);
    document.addEventListener("fullscreenchange", this._bound.fullscreen);
    document.addEventListener("webkitfullscreenchange", this._bound.fullscreen);

    this._timer = global.setInterval(function () {
      self.flush(false);
    }, this.flushIntervalMs);
    this._writingTimer = global.setInterval(function () {
      self.flushWriting("interval");
    }, WRITING_FLUSH_MS);

    // Resend whatever the previous load could not deliver for this attempt.
    this._restore();

    // Capture the device fingerprint for this session right away.
    this.deviceInfo();

    return this;
  };

  Recorder.prototype.stop = function () {
    document.removeEventListener("visibilitychange", this._bound.visibility);
    global.removeEventListener("blur", this._bound.blur);
    global.removeEventListener("focus", this._bound.focus);
    global.removeEventListener("pagehide", this._bound.pagehide);
    document.removeEventListener("fullscreenchange", this._bound.fullscreen);
    document.removeEventListener("webkitfullscreenchange", this._bound.fullscreen);
    if (this._timer) global.clearInterval(this._timer);
    if (this._writingTimer) global.clearInterval(this._writingTimer);
    this.flushWriting("stop");
    this.flush(true);
  };

  global.ExamRecorder = {
    start: function (opts) {
      return new Recorder(opts).start();
    },
    Recorder: Recorder,
  };
})(typeof window !== "undefined" ? window : this);
