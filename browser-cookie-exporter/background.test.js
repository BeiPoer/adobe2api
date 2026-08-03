const assert = require("node:assert/strict");

let listener;
let stored;
global.chrome = {
  webRequest: {
    onBeforeSendHeaders: {
      addListener(callback) {
        listener = callback;
      }
    }
  },
  storage: {
    session: {
      set(value) {
        stored = value;
      }
    }
  }
};

require("./background.js");
listener({
  tabId: 17,
  requestHeaders: [{ name: "X-Arp-Session-Id", value: "live-token" }]
});

assert.equal(stored["firefly-arp:17"].value, "live-token");
assert.equal(typeof stored["firefly-arp:17"].capturedAt, "number");
