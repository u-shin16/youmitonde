// 「よう見とんで」のページと拡張機能をつなぐ。
// ページからは拡張機能へ直接話しかけられないので、window.postMessageで受けて
// background.jsへ中継する。差し込む先はmanifestのmatchesで限定しているため、
// ここへ届くのは「よう見とんで」自身のページからのメッセージだけになる。

const PAGE_SOURCE = "youmitonde-page";
const EXTENSION_SOURCE = "youmitonde-extension";
const PORT_NAME = "youmitonde-action";

function announce() {
  window.postMessage(
    {
      source: EXTENSION_SOURCE,
      type: "ready",
      version: chrome.runtime.getManifest().version,
    },
    window.location.origin
  );
}

window.addEventListener("message", (event) => {
  if (event.source !== window) return;
  const data = event.data;
  if (!data || data.source !== PAGE_SOURCE) return;

  if (data.type === "ping") {
    announce();
    return;
  }
  if (data.type === "action") {
    startAction(data);
  }
});

function startAction({ requestId, action, targets }) {
  const reply = (message) =>
    window.postMessage({ source: EXTENSION_SOURCE, requestId, ...message }, window.location.origin);

  let port;
  try {
    port = chrome.runtime.connect({ name: PORT_NAME });
  } catch (err) {
    reply({ type: "error", error: "拡張機能へ接続できませんでした。拡張機能を読み込み直してください" });
    return;
  }

  let finished = false;
  port.onMessage.addListener((message) => {
    if (message.requestId !== requestId) return;
    if (message.type === "done" || message.type === "error") {
      finished = true;
      port.disconnect();
    }
    reply(message);
  });

  port.onDisconnect.addListener(() => {
    if (finished) return;
    // 拡張機能が更新・再読み込みされて途中で切れた場合。黙って止まらないよう伝える。
    reply({ type: "error", error: "拡張機能との接続が切れました。ページを再読み込みしてお試しください" });
  });

  port.postMessage({ type: "action", requestId, action, targets });
}

announce();
