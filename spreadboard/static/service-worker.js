self.addEventListener("push", event => {
  let payload = {};
  try { payload = event.data ? event.data.json() : {}; } catch (_error) { payload = {}; }
  const title = String(payload.title || "SpreadBoard alert").slice(0, 160);
  const options = {
    body: String(payload.body || "Open SpreadBoard for current route evidence.").slice(0, 1000),
    tag: String(payload.tag || "spreadboard-alert"),
    data: {url: String(payload.url || "/account").slice(0, 500)},
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/account", self.location.origin);
  if (target.origin !== self.location.origin) target.pathname = "/account";
  event.waitUntil(
    clients.matchAll({type: "window", includeUncontrolled: true}).then(windows => {
      const existing = windows.find(windowClient => windowClient.url === target.href);
      return existing ? existing.focus() : clients.openWindow(target.href);
    })
  );
});
