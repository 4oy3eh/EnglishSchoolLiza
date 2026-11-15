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
 *
 * Usage:
 *   const rec = ExamRecorder.start({ attemptId, endpoint });
 *   rec.setItem('q-3');                 // focus a question
 *   rec.answerChange('q-3', 'B');       // record an answer change
 *   rec.attachAudio(audioEl, 'sec-1');  // wire audio_play / audio_seek
 *
 * Transport (Phase 13 reliability rework):
 *   - Events stay in the queue until the server acknowledged them (2xx). A failed
 *     send puts them back at the FRONT of the queue and the next tick retries with
 *     exponential backoff (1 s, 2 s, 4 s … capped at 30 s). A fetch with no
 *     response after FETCH_TIMEOUT_MS (15 s) is aborted and counts as a failure.
 *   - Batches are split so each request stays under ~50 KB (sendBeacon /
 *     keepalive fetch cap is ~64 KB).
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
    var all = (this._atRisk() ? this._inflight : []).concat(this.queue);
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
          this._clearSnapshot();
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

  // -- lifecycle ------------------------------------------------------------- //
  Recorder.prototype.start = function () {
    var self = this;

    this._bound.visibility = function () {
      if (document.visibilityState === "hidden") {
        self._hiddenSince = Date.now();
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
      self.record("pagehide", {});
      self.flush(true);
    };

    document.addEventListener("visibilitychange", this._bound.visibility);
    global.addEventListener("blur", this._bound.blur);
    global.addEventListener("focus", this._bound.focus);
    global.addEventListener("pagehide", this._bound.pagehide);

    this._timer = global.setInterval(function () {
      self.flush(false);
    }, this.flushIntervalMs);

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
    if (this._timer) global.clearInterval(this._timer);
    this.flush(true);
  };

  global.ExamRecorder = {
    start: function (opts) {
      return new Recorder(opts).start();
    },
    Recorder: Recorder,
  };
})(typeof window !== "undefined" ? window : this);
