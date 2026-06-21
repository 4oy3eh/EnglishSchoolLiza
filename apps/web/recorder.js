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
 * Transport: batched; flushed on a timer, and synchronously via
 * navigator.sendBeacon on visibility-hidden / pagehide (when the page may die).
 */
(function (global) {
  "use strict";

  function nowIso() {
    return new Date().toISOString();
  }

  function Recorder(opts) {
    this.attemptId = opts.attemptId;
    // Endpoint may be templated with the attempt id, or a plain base path.
    this.endpoint =
      opts.endpoint || "/attempts/" + encodeURIComponent(this.attemptId) + "/events";
    this.flushIntervalMs = opts.flushIntervalMs || 5000;
    this.queue = [];
    this.currentItemId = null;
    this._hiddenSince = null; // ms timestamp when the tab went hidden
    this._timer = null;
    this._bound = {};
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
    return event;
  };

  // -- public capture helpers ------------------------------------------------ //
  Recorder.prototype.setItem = function (itemId) {
    this.currentItemId = itemId;
  };

  Recorder.prototype.interaction = function (itemId, payload) {
    this.record("interaction", { item_id: itemId, payload: payload });
  };

  Recorder.prototype.answerChange = function (itemId, value) {
    this.record("answer_change", {
      item_id: itemId,
      payload: { value: value },
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

  // -- transport ------------------------------------------------------------- //
  Recorder.prototype.flush = function (useBeacon) {
    if (this.queue.length === 0) return;
    var batch = { events: this.queue };
    this.queue = [];
    var body = JSON.stringify(batch);

    // sendBeacon survives page unload; use it when the page may be dying.
    if (useBeacon && global.navigator && navigator.sendBeacon) {
      var blob = new Blob([body], { type: "application/json" });
      var ok = navigator.sendBeacon(this.endpoint, blob);
      if (ok) return;
      // fall through to fetch if the beacon was rejected (e.g. too large)
    }
    if (global.fetch) {
      fetch(this.endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body,
        keepalive: true,
      }).catch(function () {
        /* swallow: telemetry must never break the exam UI */
      });
    }
  };

  // -- lifecycle ------------------------------------------------------------- //
  Recorder.prototype.start = function () {
    var self = this;

    this._bound.visibility = function () {
      if (document.visibilityState === "hidden") {
        self._hiddenSince = Date.now();
        self.record("visibility_hidden", {});
        // The tab may not come back — ship immediately.
        self.flush(true);
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
