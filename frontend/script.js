const form = document.getElementById("check-form");
const input = document.getElementById("username-input");
const button = document.getElementById("check-button");
const statusEl = document.getElementById("status");
const resultEl = document.getElementById("result");
const profileCard = document.getElementById("profile-card");
const cappedWarning = document.getElementById("capped-warning");
const cappedWarningDefaultText = cappedWarning.textContent.trim();
const authWarningEl = document.getElementById("auth-warning");
const cookieInput = document.getElementById("cookie-input");
const cookieHelpToggle = document.getElementById("cookie-help-toggle");
const cookieHelpBody = document.getElementById("cookie-help-body");
const extensionsUrlCopy = document.getElementById("extensions-url-copy");
const toastEl = document.getElementById("toast");
const REQUIRED_COOKIE_MESSAGE = "アカウントチェックにはnote.comのCookie文字列が必要です";
const CHECK_COOLDOWN_AFTER_ACTION_SECONDS = 90;
const RATE_LIMIT_FALLBACK_COOLDOWN_SECONDS = 90;
const FOLLOW_ACTION_BATCH_SIZE = 20; // 大量選択でも一気に送らず段階的に処理する
const FOLLOW_ACTION_BATCH_PAUSE_SECONDS = 15; // バッチ間の間隔（レート制限自体は未検知でも空ける。
// note.com前段のCloudFrontが約20件の連続リクエストでこちらのIPごとブロックすることが確認できたため、
// バッチ間はより長めに空けている）
const FOLLOW_ACTION_RATE_LIMIT_COOLDOWN_SECONDS = 90; // レート制限検知後、再試行までのクールダウン
const FOLLOW_ACTION_MAX_RETRY_ROUNDS = 2; // レート制限からの自動再試行の最大回数
let isChecking = false;
let checkCooldownUntil = 0;
let checkCooldownTimer = null;
let toastTimer = null;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

cookieHelpToggle.addEventListener("click", () => {
  cookieHelpBody.hidden = !cookieHelpBody.hidden;
  cookieHelpToggle.classList.toggle("open", !cookieHelpBody.hidden);
});

const DEFAULT_AVATAR =
  "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 56 56'><rect width='56' height='56' rx='28' fill='%23dbe4e1'/></svg>";

const modalOverlay = document.getElementById("account-modal-overlay");
const modalBody = document.getElementById("modal-body");
const modalClose = document.getElementById("modal-close");

modalClose.addEventListener("click", closeModal);
modalOverlay.addEventListener("click", (event) => {
  if (event.target === modalOverlay) closeModal();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!confirmOverlay.hidden) {
    resolveConfirm(false);
    return;
  }
  if (!modalOverlay.hidden) closeModal();
});

function closeModal() {
  modalOverlay.hidden = true;
  modalBody.innerHTML = "";
}

const confirmOverlay = document.getElementById("confirm-modal-overlay");
const confirmMessageEl = document.getElementById("confirm-modal-message");
const confirmOkButton = document.getElementById("confirm-modal-ok");
const confirmCancelButton = document.getElementById("confirm-modal-cancel");
let confirmResolve = null;

function showConfirm(message) {
  confirmMessageEl.textContent = message;
  confirmOverlay.hidden = false;
  return new Promise((resolve) => {
    confirmResolve = resolve;
  });
}

function resolveConfirm(result) {
  confirmOverlay.hidden = true;
  if (confirmResolve) {
    confirmResolve(result);
    confirmResolve = null;
  }
}

confirmOkButton.addEventListener("click", () => resolveConfirm(true));
confirmCancelButton.addEventListener("click", () => resolveConfirm(false));
confirmOverlay.addEventListener("click", (event) => {
  if (event.target === confirmOverlay) resolveConfirm(false);
});

async function openAccountModal(account, endpoint, actionVerb, onResolved) {
  const actionType = endpoint === "/api/unfollow" ? "unfollow" : "follow";
  modalOverlay.hidden = false;
  modalBody.innerHTML = renderModalProfile(account, null, "読み込み中…");

  let detail = null;
  try {
    const res = await fetch(`/api/creator/${encodeURIComponent(account.urlname)}`);
    if (res.ok) detail = await res.json();
  } catch (err) {
    // ネットワークエラー時も最低限の情報だけで表示を続ける
  }

  modalBody.innerHTML = renderModalProfile(account, detail, null);

  const actionButton = document.getElementById("modal-action-button");
  const actionStatus = document.getElementById("modal-action-status");
  const actionClass = endpoint === "/api/unfollow" ? "danger" : "primary";
  actionButton.classList.add(actionClass);

  actionButton.addEventListener("click", async () => {
    if (activeAction && activeAction !== actionType) {
      actionStatus.hidden = false;
      actionStatus.className = "modal-status error";
      actionStatus.textContent = "他の処理が完了するまでお待ちください";
      return;
    }

    if (!cookieInput.value.trim()) {
      actionStatus.hidden = false;
      actionStatus.className = "modal-status error";
      actionStatus.textContent = "先にCookieを入力してください";
      return;
    }

    const confirmed = await showConfirm(
      `${account.name}を${actionVerb}します。よろしいですか？\n（note.com非公式の仕組みを使っているため、失敗する場合もあります）`
    );
    if (!confirmed) return;

    actionButton.disabled = true;
    actionStatus.hidden = false;
    actionStatus.className = "modal-status";
    actionStatus.textContent = "処理中…";
    beginAction(actionType);

    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          cookieHeader: cookieInput.value.trim(),
          targets: [{ key: account.key, urlname: account.urlname }],
        }),
      });
      const data = await res.json();

      if (!res.ok) {
        actionStatus.className = "modal-status error";
        actionStatus.textContent = data.error || `${actionVerb}に失敗しました`;
        actionButton.disabled = false;
        return;
      }

      const result = data.results[0];
      if (onResolved) onResolved([result]);

      if (result.success) {
        startCheckCooldown();
        actionStatus.className = "modal-status";
        actionStatus.textContent = "完了しました。再チェックは少し待ってからできます";
        setTimeout(closeModal, 800);
      } else {
        actionStatus.className = "modal-status error";
        actionStatus.textContent = result.error || "失敗しました";
        actionButton.disabled = false;
      }
    } catch (err) {
      actionStatus.className = "modal-status error";
      actionStatus.textContent = "通信に失敗しました";
      actionButton.disabled = false;
    } finally {
      endAction();
    }
  });

  function renderModalProfile(acc, info, loadingMessage) {
    const stats = info
      ? `<div class="modal-profile__stats">フォロー中 ${info.followingCount.toLocaleString()} ・ フォロワー ${info.followerCount.toLocaleString()} ・ 記事 ${info.noteCount.toLocaleString()}</div>`
      : "";
    const bio = info && info.profile ? `<p class="modal-profile__bio">${escapeHtml(info.profile)}</p>` : "";
    const loading = loadingMessage ? `<p class="modal-status">${escapeHtml(loadingMessage)}</p>` : "";

    return `
      <div class="modal-profile">
        <img src="${acc.profileImage || DEFAULT_AVATAR}" alt="${escapeHtml(acc.name)}">
        <div class="modal-profile__name">${escapeHtml(acc.name)}</div>
        ${stats}
        ${bio}
        ${loading}
        <a class="modal-profile__link" href="${acc.noteUrl}" target="_blank" rel="noopener noreferrer">note.comで開く ↗</a>
        <div class="modal-actions">
          <button type="button" id="modal-action-button" class="modal-action-button">${actionVerb}する</button>
          <p id="modal-action-status" class="modal-status" hidden></p>
        </div>
      </div>
    `;
  }
}

function createAccountPanel({
  sectionId,
  bodyId,
  toggleId,
  listId,
  countId,
  selectAllId,
  buttonId,
  statusId,
  emptyId,
  endpoint,
  actionVerb,
}) {
  const sectionEl = document.getElementById(sectionId);
  const bodyEl = document.getElementById(bodyId);
  const toggleEl = document.getElementById(toggleId);
  const listEl = document.getElementById(listId);
  const countEl = document.getElementById(countId);
  const selectAllEl = document.getElementById(selectAllId);
  const buttonEl = document.getElementById(buttonId);
  const panelStatusEl = document.getElementById(statusId);
  const emptyEl = document.getElementById(emptyId);
  const defaultEmptyText = emptyEl.textContent;
  const actionType = endpoint === "/api/unfollow" ? "unfollow" : "follow";
  let accounts = [];
  let blocked = false;

  toggleEl.addEventListener("click", () => {
    bodyEl.hidden = !bodyEl.hidden;
    toggleEl.textContent = bodyEl.hidden ? "表示する" : "隠す";
  });

  function updateButtonState() {
    const anySelected = listEl.querySelectorAll(".account-checkbox:checked").length > 0;
    const hasCookie = cookieInput.value.trim().length > 0;
    buttonEl.disabled = blocked || !(anySelected && hasCookie);
  }

  function setBlocked(next) {
    blocked = next;
    bodyEl.classList.toggle("blocked", blocked);
    updateButtonState();
  }

  selectAllEl.addEventListener("change", () => {
    listEl.querySelectorAll(".account-checkbox").forEach((checkbox) => (checkbox.checked = selectAllEl.checked));
    updateButtonState();
  });

  listEl.addEventListener("change", (event) => {
    if (event.target.classList.contains("account-checkbox")) updateButtonState();
  });

  listEl.addEventListener("click", (event) => {
    const link = event.target.closest("a");
    if (!link) return;
    event.preventDefault();
    if (blocked) return;
    const urlname = link.closest("li").dataset.urlname;
    const account = accounts.find((a) => a.urlname === urlname);
    if (account) openAccountModal(account, endpoint, actionVerb, applyResults);
  });

  buttonEl.addEventListener("click", async () => {
    const selected = [...listEl.querySelectorAll(".account-checkbox:checked")];
    const targets = selected.map((checkbox) => {
      const account = accounts.find((a) => a.urlname === checkbox.dataset.urlname);
      return { key: account.key, urlname: account.urlname };
    });
    if (targets.length === 0) return;

    const confirmed = await showConfirm(
      `${targets.length}件を${actionVerb}します。よろしいですか？\n（note.com非公式の仕組みを使っているため、失敗する場合もあります。件数が多い時は自動的に段階的に処理します）`
    );
    if (!confirmed) return;

    buttonEl.disabled = true;
    panelStatusEl.hidden = false;
    panelStatusEl.className = "status";
    panelStatusEl.textContent = `処理中…（0/${targets.length}件）`;
    beginAction(actionType);

    try {
      const { successCount, totalCount } = await runFollowActionInBatches(targets);
      if (successCount > 0) startCheckCooldown();
      panelStatusEl.className = "status";
      panelStatusEl.textContent = `${successCount}/${totalCount}件の${actionVerb}に成功しました。再チェックは少し待ってからできます`;
    } catch (err) {
      panelStatusEl.className = "status error";
      panelStatusEl.textContent = "通信に失敗しました。時間をおいてもう一度お試しください";
    } finally {
      endAction();
    }
  });

  // 一気に全件送るとnote.comのレート制限に引っかかりやすく、大量選択時は
  // 途中から一律失敗になりがちだった。少数ずつ間隔を空けて送り、レート制限に
  // 引っかかった分だけクールダウン後に自動で再試行することで、大量選択でも
  // 段階的に、粘り強く処理する。
  async function runFollowActionInBatches(targets) {
    const finalResults = new Map(); // urlname -> 最新の結果
    let pending = targets;
    let round = 0;

    while (pending.length > 0 && round <= FOLLOW_ACTION_MAX_RETRY_ROUNDS) {
      if (round > 0) {
        panelStatusEl.textContent =
          `レート制限を検知したため、${FOLLOW_ACTION_RATE_LIMIT_COOLDOWN_SECONDS}秒待ってから` +
          `残り${pending.length}件を再試行します…`;
        await sleep(FOLLOW_ACTION_RATE_LIMIT_COOLDOWN_SECONDS * 1000);
      }

      const rateLimited = [];
      for (let i = 0; i < pending.length; i += FOLLOW_ACTION_BATCH_SIZE) {
        const batch = pending.slice(i, i + FOLLOW_ACTION_BATCH_SIZE);
        panelStatusEl.textContent = `処理中…（${finalResults.size}/${targets.length}件完了）`;

        let res, data;
        try {
          res = await fetch(endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ cookieHeader: cookieInput.value.trim(), targets: batch }),
          });
          data = await res.json();
        } catch (err) {
          batch.forEach((t) =>
            finalResults.set(t.urlname, { urlname: t.urlname, success: false, error: "通信に失敗しました" })
          );
          applyResults(batch.map((t) => finalResults.get(t.urlname)));
          pending = [];
          break;
        }

        if (!res.ok) {
          const message = data.error || `${actionVerb}に失敗しました`;
          batch.forEach((t) => finalResults.set(t.urlname, { urlname: t.urlname, success: false, error: message }));
          applyResults(batch.map((t) => finalResults.get(t.urlname)));
          pending = [];
          break;
        }

        data.results.forEach((result) => {
          finalResults.set(result.urlname, result);
          if (!result.success && (result.error || "").includes("レート制限")) {
            const target = batch.find((t) => t.urlname === result.urlname);
            if (target) rateLimited.push(target);
          }
        });
        applyResults(data.results);

        const isLastBatch = i + FOLLOW_ACTION_BATCH_SIZE >= pending.length;
        if (!isLastBatch) await sleep(FOLLOW_ACTION_BATCH_PAUSE_SECONDS * 1000);
      }

      pending = rateLimited;
      round += 1;
    }

    const results = [...finalResults.values()];
    return { successCount: results.filter((r) => r.success).length, totalCount: targets.length };
  }

  function applyResults(results) {
    results.forEach((result) => {
      const row = listEl.querySelector(`li[data-urlname="${cssEscape(result.urlname)}"]`);
      if (!row) return;

      const rowStatus = row.querySelector(".row-status");
      if (result.success) {
        row.classList.add("done");
        row.querySelector(".account-checkbox").disabled = true;
        row.querySelector(".account-checkbox").checked = false;
        rowStatus.textContent = "完了";
        rowStatus.classList.remove("error");
      } else {
        rowStatus.textContent = result.error || "失敗";
        rowStatus.classList.add("error");
      }
    });
  }

  return {
    render(newAccounts) {
      accounts = newAccounts;
      selectAllEl.checked = false;
      panelStatusEl.hidden = true;
      bodyEl.hidden = true;
      toggleEl.textContent = "表示する";
      emptyEl.textContent = defaultEmptyText;
      emptyEl.classList.remove("warning");

      if (newAccounts.length === 0) {
        sectionEl.hidden = true;
        emptyEl.hidden = false;
        countEl.textContent = "";
        return false;
      }

      emptyEl.hidden = true;
      sectionEl.hidden = false;
      countEl.textContent = `（${newAccounts.length}人）`;
      listEl.innerHTML = newAccounts
        .map(
          (account) => `
          <li data-urlname="${escapeHtml(account.urlname)}">
            <input type="checkbox" class="account-checkbox" data-urlname="${escapeHtml(account.urlname)}">
            <img src="${account.profileImage || DEFAULT_AVATAR}" alt="${escapeHtml(account.name)}">
            <a href="${account.noteUrl}" target="_blank" rel="noopener noreferrer">${escapeHtml(account.name)}</a>
            <span class="row-status"></span>
          </li>
        `
        )
        .join("");
      updateButtonState();
      return true;
    },
    renderUnavailable(message) {
      accounts = [];
      selectAllEl.checked = false;
      panelStatusEl.hidden = true;
      bodyEl.hidden = true;
      toggleEl.textContent = "表示する";
      listEl.innerHTML = "";
      sectionEl.hidden = true;
      countEl.textContent = "";
      emptyEl.textContent = message;
      emptyEl.classList.add("warning");
      emptyEl.hidden = false;
      updateButtonState();
      return false;
    },
    renderHidden() {
      accounts = [];
      selectAllEl.checked = false;
      panelStatusEl.hidden = true;
      bodyEl.hidden = true;
      toggleEl.textContent = "表示する";
      listEl.innerHTML = "";
      sectionEl.hidden = true;
      countEl.textContent = "";
      emptyEl.hidden = true;
      updateButtonState();
      return false;
    },
    refreshButtonState: updateButtonState,
    setBlocked,
  };
}

const unfollowPanel = createAccountPanel({
  sectionId: "not-following-back-section",
  bodyId: "not-following-back-body",
  toggleId: "not-following-back-toggle",
  listId: "not-following-back-list",
  countId: "not-following-back-count",
  selectAllId: "select-all-unfollow",
  buttonId: "unfollow-button",
  statusId: "unfollow-status",
  emptyId: "empty-not-following-back",
  endpoint: "/api/unfollow",
  actionVerb: "フォロー解除",
});

const followPanel = createAccountPanel({
  sectionId: "to-follow-back-section",
  bodyId: "to-follow-back-body",
  toggleId: "to-follow-back-toggle",
  listId: "to-follow-back-list",
  countId: "to-follow-back-count",
  selectAllId: "select-all-follow",
  buttonId: "follow-button",
  statusId: "follow-status",
  emptyId: "empty-to-follow-back",
  endpoint: "/api/follow",
  actionVerb: "フォロー",
});

let activeAction = null;

function beginAction(type) {
  activeAction = type;
  // Only block the opposite panel; the active panel disables its own
  // button directly, and re-running its updateButtonState here would
  // undo that.
  if (type === "follow") {
    unfollowPanel.setBlocked(true);
  } else {
    followPanel.setBlocked(true);
  }
}

function endAction() {
  activeAction = null;
  unfollowPanel.setBlocked(false);
  followPanel.setBlocked(false);
}

function getRemainingCheckCooldownSeconds() {
  return Math.max(0, Math.ceil((checkCooldownUntil - Date.now()) / 1000));
}

function updateCheckButtonState() {
  if (checkCooldownTimer) {
    clearTimeout(checkCooldownTimer);
    checkCooldownTimer = null;
  }

  if (isChecking) {
    button.disabled = true;
    button.textContent = "チェック中…";
    return;
  }

  if (!input.value.trim() || !cookieInput.value.trim()) {
    button.disabled = true;
    button.textContent = "チェックする";
    return;
  }

  const remainingSeconds = getRemainingCheckCooldownSeconds();
  if (remainingSeconds > 0) {
    button.disabled = true;
    button.textContent = `再チェック ${remainingSeconds}秒後`;
    checkCooldownTimer = setTimeout(updateCheckButtonState, 1000);
    return;
  }

  button.disabled = false;
  button.textContent = "チェックする";
}

function startCheckCooldown(seconds = CHECK_COOLDOWN_AFTER_ACTION_SECONDS) {
  checkCooldownUntil = Math.max(checkCooldownUntil, Date.now() + seconds * 1000);
  updateCheckButtonState();
}

async function readJsonResponse(res) {
  const text = await res.text();
  if (!text) return {};

  try {
    return JSON.parse(text);
  } catch (err) {
    return {
      parseError: true,
      error: res.ok
        ? "サーバーの応答を読み取れませんでした。時間をおいてもう一度お試しください"
        : "サーバー側でエラーが発生しました。時間をおいてもう一度お試しください",
    };
  }
}

function syncCookieValidity() {
  cookieInput.setCustomValidity(cookieInput.value.trim() ? "" : REQUIRED_COOKIE_MESSAGE);
}

function showCookieGuidance() {
  showError("Cookieを貼り付けてからチェックしてください。下の案内から拡張機能を使うと簡単にコピーできます");
  cookieHelpBody.hidden = false;
  cookieHelpToggle.classList.add("open");
  cookieInput.focus();
  cookieInput.scrollIntoView({ behavior: "smooth", block: "center" });
}

syncCookieValidity();

input.addEventListener("input", updateCheckButtonState);

cookieInput.addEventListener("input", () => {
  syncCookieValidity();
  updateCheckButtonState();
  unfollowPanel.refreshButtonState();
  followPanel.refreshButtonState();
});

cookieInput.addEventListener("invalid", () => {
  showCookieGuidance();
});

extensionsUrlCopy.addEventListener("click", async () => {
  const extensionsUrl = "chrome://extensions";
  try {
    await navigator.clipboard.writeText(extensionsUrl);
    showToast("コピーしました");
  } catch (err) {
    showToast("コピーできませんでした。chrome://extensions を手入力してください", true);
  }
});

const USERNAME_HISTORY_KEY = "youmitonde:usernameHistory";
const USERNAME_HISTORY_MAX = 5;
const usernameHistoryList = document.getElementById("username-history");

function loadUsernameHistory() {
  try {
    const raw = JSON.parse(localStorage.getItem(USERNAME_HISTORY_KEY));
    return Array.isArray(raw) ? raw : [];
  } catch (err) {
    return [];
  }
}

function renderUsernameHistory(history) {
  usernameHistoryList.innerHTML = history.map((name) => `<option value="${escapeHtml(name)}">`).join("");
}

function rememberUsername(username) {
  const history = [username, ...loadUsernameHistory().filter((name) => name !== username)].slice(
    0,
    USERNAME_HISTORY_MAX
  );
  localStorage.setItem(USERNAME_HISTORY_KEY, JSON.stringify(history));
  renderUsernameHistory(history);
}

const usernameHistory = loadUsernameHistory();
renderUsernameHistory(usernameHistory);
if (usernameHistory[0]) {
  input.value = usernameHistory[0];
}
updateCheckButtonState();

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const username = input.value.trim();
  if (!username) return;
  const cookieHeader = cookieInput.value.trim();
  syncCookieValidity();
  if (!cookieHeader) {
    showCookieGuidance();
    cookieInput.reportValidity();
    return;
  }
  const remainingCooldownSeconds = getRemainingCheckCooldownSeconds();
  if (remainingCooldownSeconds > 0) {
    showError(`フォロー操作の直後はnote.comがレート制限しやすいため、あと${remainingCooldownSeconds}秒ほど待ってから再チェックしてください`);
    return;
  }

  rememberUsername(username);

  setLoading(true);
  hideAll();

  try {
    const res = await fetch("/api/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username,
        cookieHeader,
      }),
    });
    const data = await readJsonResponse(res);

    if (!res.ok || data.parseError) {
      if (res.status === 429 || res.status === 503) {
        startCheckCooldown(data.retryAfterSeconds || RATE_LIMIT_FALLBACK_COOLDOWN_SECONDS);
      }
      showError(data.error || "エラーが発生しました");
      return;
    }

    renderResult(data);
  } catch (err) {
    showError("通信に失敗しました。時間をおいてもう一度お試しください");
  } finally {
    setLoading(false);
  }
});

function setLoading(isLoading) {
  isChecking = isLoading;
  updateCheckButtonState();
  if (isLoading) {
    statusEl.hidden = false;
    statusEl.className = "status";
    statusEl.textContent = "note.comを確認中です。フォローが多いと時間がかかる場合があります…";
  }
}

function hideAll() {
  resultEl.hidden = true;
  cappedWarning.hidden = true;
  authWarningEl.hidden = true;
  unfollowPanel.render([]);
  followPanel.render([]);
  profileCard.innerHTML = "";
}

function showError(message) {
  statusEl.hidden = false;
  statusEl.className = "status error";
  statusEl.textContent = message;
}

function showToast(message, isError = false) {
  if (toastTimer) {
    clearTimeout(toastTimer);
    toastTimer = null;
  }
  toastEl.textContent = message;
  toastEl.classList.toggle("error", isError);
  toastEl.hidden = false;
  toastTimer = setTimeout(() => {
    toastEl.hidden = true;
  }, 2200);
}

function renderResult(data) {
  statusEl.hidden = true;
  resultEl.hidden = false;

  const creator = data.creator;
  profileCard.innerHTML = `
    <img src="${creator.profileImage || DEFAULT_AVATAR}" alt="${escapeHtml(creator.name || "")}">
    <div>
      <div class="profile-card__name">${escapeHtml(creator.name || creator.urlname)}</div>
      <div class="profile-card__stats">
        フォロー中 ${creator.followingCount.toLocaleString()} ・ フォロワー ${creator.followerCount.toLocaleString()}
        （確認済み: フォロー中 ${data.checkedFollowingCount} 件 / フォロワー ${data.checkedFollowerCount} 件）
      </div>
    </div>
  `;

  if (data.authWarning) {
    // A cookie/account mismatch makes every other capped/reliability note
    // moot until it's fixed, so show only this one message.
    authWarningEl.textContent = `🔑 ${data.authWarning}`;
    authWarningEl.hidden = false;
    unfollowPanel.renderHidden();
    followPanel.renderHidden();
    return;
  }

  if (data.capped) {
    cappedWarning.textContent = cappedWarningDefaultText;
    cappedWarning.hidden = false;
  }

  if (data.notFollowingBackReliable === false) {
    unfollowPanel.renderUnavailable(
      "フォロワー一覧がnote.com側の上限で一部しか取得できないため、フォローバックされていない人は正確に判定できません。"
    );
  } else {
    unfollowPanel.render(data.notFollowingBack);
  }

  if (data.toFollowBackReliable === false) {
    followPanel.renderUnavailable(
      data.toFollowBackUnavailableReason ||
        "フォロー返し候補を正確に判定できないため、この一覧は表示しません。"
    );
  } else {
    followPanel.render(data.toFollowBack);
  }
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function cssEscape(str) {
  return window.CSS && CSS.escape ? CSS.escape(str) : String(str).replace(/"/g, '\\"');
}
