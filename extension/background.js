// フォロー/フォロー解除の通信を、サーバーではなく利用者自身のChromeから送る。
//
// もともとはVPSのFlaskがnote.comのAPIを叩いていた。note.comの前段にいるCloudFrontは
// IP単位でリクエスト数を見ており、チェック処理だけで数千回投げたあとに解除を始めるため、
// 100件選んでも5件ほどでサーバーのIPごとブロックされ、残りが一律エラーになっていた。
// ここから送れば利用者自身のIP・自身のセッションになるので、note.comを普通に
// 操作しているのと同じ扱いになり、件数の壁がなくなる。
//
// fetchはService Workerからではなく、note.comのタブへ差し込んで実行する。
// Service Workerからのfetchはnote.comにとって別オリジンからの呼び出しで、
// Origin/RefererはfetchのAPI上変更できず、SameSite Cookieの扱いも保証がない。
// note.comのタブの中で実行すれば同一オリジンになり、Cookie・Origin・Refererが
// すべて本物と同じになる。

const PORT_NAME = "youmitonde-action";
const FOLLOW_API_BASE = "https://note.com/api/v3/users";
const ACTION_DELAY_MS = 1200; // note.com自身の429に当たらない程度に間隔を空ける
const RETRY_DELAY_MS = 5000; // 429/5xxを1回だけ待って投げ直す
const MAX_TARGETS = 500;
const TAB_READY_TIMEOUT_MS = 20000;
const TAB_POLL_INTERVAL_MS = 300;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== PORT_NAME) return;

  const state = { disconnected: false };
  port.onDisconnect.addListener(() => {
    state.disconnected = true;
  });

  port.onMessage.addListener((message) => {
    if (!message || message.type !== "action") return;
    runAction(message, port, state).catch((err) => {
      post(port, state, { type: "error", requestId: message.requestId, error: err.message });
    });
  });
});

function post(port, state, message) {
  if (state.disconnected) return;
  try {
    port.postMessage(message);
  } catch (err) {
    state.disconnected = true;
  }
}

async function runAction({ requestId, action, targets }, port, state) {
  if (action !== "follow" && action !== "unfollow") throw new Error("不明な操作です");
  if (!Array.isArray(targets) || targets.length === 0) throw new Error("対象が選択されていません");
  if (targets.length > MAX_TARGETS) throw new Error(`一度に処理できるのは${MAX_TARGETS}件までです`);

  const method = action === "follow" ? "POST" : "DELETE";
  const tab = await acquireNoteTab();

  try {
    for (let index = 0; index < targets.length; index += 1) {
      if (state.disconnected) return; // 画面が閉じられた。これ以上note.comを叩かない
      if (index > 0) await sleep(ACTION_DELAY_MS);
      const result = await sendFollowRequest(tab.id, targets[index], method);
      post(port, state, { type: "progress", requestId, result });
    }
  } finally {
    if (tab.created) {
      try {
        await chrome.tabs.remove(tab.id);
      } catch (err) {
        // 利用者が先に閉じた場合。何もしない
      }
    }
  }

  post(port, state, { type: "done", requestId });
}

async function acquireNoteTab() {
  // すでに開いているnote.comのタブがあれば借りる。無ければ裏で開いて、
  // 処理が終わったら自分で閉じる（利用者のタブは閉じない）。
  const tabs = await chrome.tabs.query({ url: "https://note.com/*", status: "complete" });
  if (tabs.length > 0) return { id: tabs[0].id, created: false };

  const tab = await chrome.tabs.create({ url: "https://note.com/", active: false });
  await waitForTabReady(tab.id);
  return { id: tab.id, created: true };
}

async function waitForTabReady(tabId) {
  const deadline = Date.now() + TAB_READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    let tab;
    try {
      tab = await chrome.tabs.get(tabId);
    } catch (err) {
      throw new Error("note.comのタブを開けませんでした");
    }
    if (tab.status === "complete") return;
    await sleep(TAB_POLL_INTERVAL_MS);
  }
  throw new Error("note.comの読み込みに時間がかかっています。時間をおいてもう一度お試しください");
}

async function sendFollowRequest(tabId, target, method) {
  const key = target && target.key;
  const urlname = (target && target.urlname) || null;
  if (!key) return { urlname, success: false, error: "keyが取得できませんでした" };

  const url = `${FOLLOW_API_BASE}/${encodeURIComponent(key)}/following`;
  let response = await callInNoteTab(tabId, url, method);

  if (response.status === 429 || (response.status >= 500 && response.status < 600)) {
    await sleep(RETRY_DELAY_MS);
    response = await callInNoteTab(tabId, url, method);
  }

  return { urlname, ...describeResponse(response) };
}

async function callInNoteTab(tabId, url, method) {
  try {
    const [injected] = await chrome.scripting.executeScript({
      target: { tabId },
      world: "ISOLATED",
      func: callFollowApi,
      args: [url, method],
    });
    return (injected && injected.result) || { status: 0, error: "応答がありませんでした" };
  } catch (err) {
    return { status: 0, error: "note.comのタブへ接続できませんでした" };
  }
}

// note.comのタブの中で実行される。外の変数を参照してはいけない。
async function callFollowApi(url, method) {
  try {
    const response = await fetch(url, {
      method,
      credentials: "include",
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });
    return { status: response.status };
  } catch (err) {
    return { status: 0, error: (err && err.message) || "通信に失敗しました" };
  }
}

function describeResponse(response) {
  const { status } = response;
  if (status === 200 || status === 201 || status === 204) return { success: true, error: null };
  if (status === 0) {
    return { success: false, error: response.error || "note.comへの接続に失敗しました" };
  }
  if (status === 401 || status === 403) {
    return {
      success: false,
      error: "note.comにログインしていないようです。note.comを開いてログインし直してからお試しください",
    };
  }
  if (status === 404) return { success: false, error: "アカウントが見つかりませんでした" };
  if (status === 429) {
    return {
      success: false,
      error: "note.comのレート制限に達しました。少し待ってからもう一度お試しください",
    };
  }
  return { success: false, error: `note.comがstatus ${status}を返しました` };
}
