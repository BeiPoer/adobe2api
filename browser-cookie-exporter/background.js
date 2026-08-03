const ARP_CAPTURE_KEY_PREFIX = "firefly-arp:";

chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const header = (details.requestHeaders || []).find(
      (item) => String(item.name || "").toLowerCase() === "x-arp-session-id"
    );
    const value = String((header && header.value) || "").trim();
    if (!value) return;

    chrome.storage.session.set({
      [`${ARP_CAPTURE_KEY_PREFIX}${details.tabId}`]: {
        value,
        capturedAt: Date.now()
      }
    });
  },
  { urls: ["https://firefly-3p.ff.adobe.io/*"] },
  ["requestHeaders", "extraHeaders"]
);
