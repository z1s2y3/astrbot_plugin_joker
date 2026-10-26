(() => {
  "use strict";

  // The server stores only approved local image endpoints in these data
  // attributes.  Validate them again before using them in CSS so a future
  // template change cannot turn a background preference into an external URL
  // fetch or a CSS injection point.
  const validThemeAssetUrl = (value) => {
    if (!value) return "";
    try {
      const url = new URL(value, window.location.origin);
      return url.origin === window.location.origin
        && /^\/theme\/assets\/\d+$/.test(url.pathname)
        ? url.pathname
        : "";
    } catch (_) {
      return "";
    }
  };

  const applyPortalAppearance = () => {
    const body = document.body;
    const background = document.querySelector(".theme-background");
    if (!body || !background) return;
    const requestedMode = body.dataset.themeMode || "auto";
    const mode = ["auto", "day", "night"].includes(requestedMode)
      ? requestedMode
      : "auto";
    const hour = new Date().getHours();
    const period = mode === "auto" ? (hour >= 6 && hour < 18 ? "day" : "night") : mode;
    const dayUrl = validThemeAssetUrl(body.dataset.themeDay);
    const nightUrl = validThemeAssetUrl(body.dataset.themeNight);
    // If a customer selected only one image, keep it visible in both periods
    // instead of unexpectedly falling back to the built-in background.
    const imageUrl = period === "day" ? (dayUrl || nightUrl) : (nightUrl || dayUrl);

    document.documentElement.dataset.themePeriod = period;
    if (imageUrl) {
      background.style.backgroundImage = `url("${imageUrl}")`;
      body.classList.add("has-theme-background");
    } else {
      background.style.removeProperty("background-image");
      body.classList.remove("has-theme-background");
    }
  };

  applyPortalAppearance();
  // Keep an already-open dashboard in sync when local time crosses the
  // day/night boundary; this has no server request and does not affect bot
  // synchronization.
  window.setInterval(applyPortalAppearance, 60 * 1000);

  document.querySelectorAll("[data-clear-member-prefix]").forEach((button) => {
    button.addEventListener("click", () => {
      const form = button.closest("form");
      const input = form?.querySelector("[name='member_name_prefix']");
      if (!input) return;
      input.value = "";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.focus();
    });
  });

  const bindBulkSelection = (formSelector, checkboxSelector, inputSelector, submitSelector) => {
    document.querySelectorAll(formSelector).forEach((form) => {
      if (!(form instanceof HTMLFormElement)) return;
      const inputContainer = form.querySelector(inputSelector);
      const submit = form.querySelector(submitSelector);
      const checkboxes = Array.from(document.querySelectorAll(checkboxSelector))
        .filter((item) => item instanceof HTMLInputElement);
      const sync = () => {
        const selected = checkboxes
          .filter((item) => item.checked && item.value)
          .map((item) => item.value);
        if (inputContainer instanceof HTMLElement) {
          inputContainer.replaceChildren();
          selected.forEach((userId) => {
            const input = document.createElement("input");
            input.type = "hidden";
            input.name = "user_id";
            input.value = userId;
            inputContainer.append(input);
          });
        }
        if (submit instanceof HTMLButtonElement) submit.disabled = selected.length === 0;
      };
      checkboxes.forEach((checkbox) => checkbox.addEventListener("change", sync));
      sync();
    });
  };
  bindBulkSelection(
    "[data-blacklist-bulk-form]",
    "[data-blacklist-select]",
    "[data-blacklist-bulk-inputs]",
    "[data-blacklist-bulk-submit]",
  );
  bindBulkSelection(
    "[data-global-blacklist-bulk-form]",
    "[data-global-blacklist-select]",
    "[data-global-blacklist-bulk-inputs]",
    "[data-global-blacklist-bulk-submit]",
  );

  // Keep navigation server-rendered (so CSRF and flash messages remain
  // reliable), but make slow network hops obvious and prevent duplicate form
  // submissions when a reverse proxy takes a moment to respond.
  const showBusy = (element, label) => {
    if (!element || element.dataset.busy === "1") return;
    document.documentElement.classList.add("is-navigating");
    element.dataset.busy = "1";
    element.setAttribute("aria-busy", "true");
    if (element.tagName === "BUTTON") {
      element.dataset.originalLabel = element.textContent || "";
      element.textContent = label || "处理中…";
      element.disabled = true;
    }
  };

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[href]");
    if (!link || event.defaultPrevented || link.target === "_blank") return;
    if (link.origin !== window.location.origin || link.href.includes("#")) return;
    showBusy(link, "加载中…");
  }, { capture: true });

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  const submitter = event.submitter || form.querySelector("button[type=submit]");
  const confirmation = submitter?.dataset.confirm || form.dataset.confirm;
  if (confirmation && !window.confirm(confirmation)) {
    event.preventDefault();
    return;
    }
    // A second Enter/click can arrive before the first navigation unloads the
    // page.  Returning alone would still let that second native submit pass
    // through, so explicitly cancel it to keep retry-safe saves single-shot.
    if (form.dataset.busy === "1") {
      event.preventDefault();
      return;
    }
  form.dataset.busy = "1";
  form.setAttribute("aria-busy", "true");
  // Disabled submit buttons are excluded from native form serialization.
  // Preserve a clicked button's name/value (for example lottery
  // operation=update/delete) in a hidden control before showing the busy
  // state, otherwise the server receives an empty operation.
  if (submitter?.name && submitter.value) {
    const hasHiddenValue = Array.from(form.elements).some((element) => (
      element !== submitter && element.type === "hidden" && element.name === submitter.name
    ));
    if (!hasHiddenValue) {
      const proxy = document.createElement("input");
      proxy.type = "hidden";
      proxy.name = submitter.name;
      proxy.value = submitter.value;
      proxy.dataset.submitProxy = "1";
      form.appendChild(proxy);
    }
  }
  showBusy(submitter, submitter?.dataset.busyLabel || "提交中…");
}, { capture: true });

  document.querySelectorAll("[data-generate-sync-token]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = button.closest("label")?.querySelector("[data-sync-token-input]");
      if (!(input instanceof HTMLInputElement)) return;
      if (!window.crypto?.getRandomValues) {
        button.textContent = "请手动填写";
        return;
      }
      const bytes = new Uint8Array(36);
      window.crypto.getRandomValues(bytes);
      input.value = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
      input.type = "text";
      input.focus();
      button.textContent = "已生成";
      window.setTimeout(() => { button.textContent = "生成随机令牌"; }, 1800);
    });
  });

  // Dashboard anchors share the /user route, so server-side path matching
  // would otherwise keep highlighting “我的群组” after clicking “同步状态”.
  // Keep anchor state local to this page and leave group-page navigation
  // independent.
  const dashboardAnchorLinks = Array.from(
    document.querySelectorAll("[data-dashboard-nav]")
  ).filter((link) => link instanceof HTMLAnchorElement);
  if (dashboardAnchorLinks.length) {
    const syncDashboardNavigation = () => {
      const hash = window.location.hash.replace(/^#/, "");
      const selected = hash
        ? dashboardAnchorLinks.find(
          (link) => link.dataset.dashboardAnchor === hash
        )
        : dashboardAnchorLinks.find(
          (link) => link.dataset.dashboardNav === "groups"
        );
      dashboardAnchorLinks.forEach((link) => {
        const active = link === selected;
        link.classList.toggle("active", active);
        if (active) link.setAttribute("aria-current", "page");
        else link.removeAttribute("aria-current");
      });
    };
    window.addEventListener("hashchange", syncDashboardNavigation);
    dashboardAnchorLinks.forEach((link) => {
      link.addEventListener("click", () => {
        window.setTimeout(syncDashboardNavigation, 0);
      });
    });
    syncDashboardNavigation();
  }

  // A group page has one independent panel for every management function.
  // Navigation only changes the visible panel and never sends a request.  A
  // draft is reset when the user leaves a panel; the native POST form remains
  // the only persistence boundary, so Windows receives a new revision only
  // after the matching Save button is submitted.
  const groupWorkspace = document.querySelector("[data-group-workspace]");
  if (groupWorkspace instanceof HTMLElement) {
    // On a phone the server-rendered page can contain a large number of
    // management panels.  Keep the initial navigation compact until this
    // controller has selected the requested panel; the CSS fallback only
    // applies before this marker is set and does not affect desktop/no-JS
    // rendering.
    const compactViewport = () => (
      typeof window.matchMedia === "function"
      && window.matchMedia("(max-width: 900px)").matches
    );
    const collapseCompactNavigation = (activePanel) => {
      if (!compactViewport()) return;
      const groupNav = document.querySelector(".group-sidebar-nav");
      if (!(groupNav instanceof HTMLElement)) return;
      groupNav.querySelectorAll("details").forEach((branch) => {
        branch.open = false;
      });
      // Keep the root tree visible and reopen only the branch containing the
      // selected item.  This prevents a long stack of expanded categories on
      // narrow screens while preserving the desktop tree behavior.
      groupNav.querySelector(":scope > details")?.setAttribute("open", "");
      openNavigationBranches(activePanel);
    };
    const panels = Array.from(
      groupWorkspace.querySelectorAll("[data-group-section-panel]")
    ).filter((panel) => panel instanceof HTMLElement);
    const switches = Array.from(
      document.querySelectorAll("[data-group-section-switch]")
    ).filter((link) => link instanceof HTMLAnchorElement);
    const draftForms = Array.from(document.querySelectorAll(
      "form[data-group-draft-form], form[data-feature-form], form[data-timed-task-form], " +
      "form[data-reply-form], form[data-sub-admin-form], form[data-player-reply-form]"
    )).filter((form, index, all) => (
      form instanceof HTMLFormElement && all.indexOf(form) === index
    ));
    let activePanel = null;

    const panelForKey = (key) => panels.find(
      (panel) => panel.dataset.groupSectionPanel === key
    ) || null;

    const firstFeaturePanel = () => panels.find(
      (panel) => panel.id.startsWith("feature-")
    ) || null;

    // Management endpoints used these anchors before the corresponding
    // feature switches were merged into their dedicated panels.  Preserve
    // old post-save redirects and bookmarked links while using one panel per
    // group-management function.
    const legacySectionAliases = {
      "timed-messages": "feature-timed_message",
      "auto-replies": "feature-reply",
      "sub-admins": "feature-sub_admin",
      "player-replies": "feature-player_reply",
    };
    const normalizedKey = (requestedKey) => {
      const key = legacySectionAliases[requestedKey] || requestedKey;
      if (key === "feature-settings") {
        return firstFeaturePanel()?.dataset.groupSectionPanel || "group-overview";
      }
      return panelForKey(key) ? key : "group-overview";
    };

    const discardDraftForms = () => {
      draftForms.forEach((form) => {
        if (form.dataset.busy === "1") return;
        // Reset every draft, including values filled by an edit button.  This
        // keeps programmatic edits and file selections from surviving a panel
        // switch without needing a local cache or an extra API call.
        form.reset();
        form.dataset.dirty = "0";
      });
    };

    const openNavigationBranches = (panel) => {
      if (!(panel instanceof HTMLElement)) return;
      const targetId = panel.id;
      switches.forEach((link) => {
        if (link.dataset.groupSectionSwitch !== targetId) return;
        // A feature can be nested (for example, blacklist settings/data).
        // Open every details ancestor so a deep link never leaves the active
        // item hidden behind a collapsed outer tree.
        let branch = link.parentElement?.closest("details");
        while (branch) {
          branch.open = true;
          branch = branch.parentElement?.closest("details");
        }
      });
    };

    const showSection = (requestedKey, options = {}) => {
      const key = normalizedKey(requestedKey);
      const next = panelForKey(key) || panels[0] || null;
      if (!(next instanceof HTMLElement)) return null;
      if (activePanel && activePanel !== next && options.discard !== false) {
        discardDraftForms();
      }
      panels.forEach((panel) => {
        const selected = panel === next;
        panel.hidden = !selected;
        panel.setAttribute("aria-hidden", selected ? "false" : "true");
      });
      // A deep link or a post-save redirect can point at a nested feature.
      // Keep the tree branch expanded so the active item is visible without
      // forcing the user to hunt through collapsed navigation nodes.
      openNavigationBranches(next);
      collapseCompactNavigation(next);
      switches.forEach((link) => {
        const selected = link.dataset.groupSectionSwitch === next.dataset.groupSectionPanel;
        link.classList.toggle("active", selected);
        if (selected) {
          link.setAttribute("aria-current", "page");
          link.setAttribute("aria-controls", next.id);
        } else {
          link.removeAttribute("aria-current");
        }
      });
      activePanel = next;
      return next;
    };

    draftForms.forEach((form) => {
      const markDirty = () => { form.dataset.dirty = "1"; };
      form.addEventListener("input", markDirty);
      form.addEventListener("change", markDirty);
      form.addEventListener("reset", () => { form.dataset.dirty = "0"; });
    });

    const hashKey = () => {
      const raw = window.location.hash.replace(/^#/, "");
      if (!raw) return "group-overview";
      try {
        return decodeURIComponent(raw);
      } catch (_) {
        return raw;
      }
    };

    const selectFromHash = (discard) => {
      const requestedKey = hashKey();
      const directAnchor = document.getElementById(requestedKey);
      const ownerPanel = directAnchor?.closest("[data-group-section-panel]");
      const next = panelForKey(requestedKey)
        ? showSection(requestedKey, { discard })
        : ownerPanel instanceof HTMLElement
          ? showSection(ownerPanel.dataset.groupSectionPanel, { discard })
          : showSection(requestedKey, { discard });
      if (
        directAnchor instanceof HTMLElement
        && ownerPanel instanceof HTMLElement
        && directAnchor !== ownerPanel
      ) {
        window.setTimeout(() => directAnchor.scrollIntoView({ block: "start" }), 0);
        return;
      }
      if (next && hashKey() !== next.dataset.groupSectionPanel) {
        history.replaceState(null, "", `#${encodeURIComponent(next.dataset.groupSectionPanel)}`);
      }
    };

    switches.forEach((link) => {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        const key = link.dataset.groupSectionSwitch || "group-overview";
        const next = showSection(key, { discard: true });
        if (!next) return;
        history.replaceState(null, "", `#${encodeURIComponent(next.dataset.groupSectionPanel)}`);
        next.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });

    window.addEventListener("hashchange", () => selectFromHash(true));
    // The mobile CSS hides non-overview panels until the controller has
    // selected one, avoiding a flash of the full configuration page while
    // deferred JavaScript is loading.
    groupWorkspace.dataset.panelReady = "1";
    selectFromHash(false);
  }

  // The lottery feature keeps award rows in a small, dedicated form rather
  // than expanding the generic feature schema.  This preserves compatibility
  // with older clients while making name/probability/stock/enabled editable in
  // one table.  The action form is populated only when a row button is
  // clicked, so unsaved edits never leak into a generic feature save.
  document.querySelectorAll("[data-lottery-awards]").forEach((editor) => {
    if (!(editor instanceof HTMLElement)) return;
    const actionForm = editor.querySelector("form[data-lottery-award-action-form]");
    if (!(actionForm instanceof HTMLFormElement)) return;
    const setValue = (key, value) => {
      const field = actionForm.querySelector(`[name="${key}"]`);
      if (field) field.value = value == null ? "" : String(value);
    };
    const submitAction = (operation, row) => {
      setValue("operation", operation);
      setValue("award_id", row?.dataset.awardId || "");
      if (row) {
        setValue("name", row.querySelector('[name="name"]')?.value || "");
        setValue("probability", row.querySelector('[name="probability"]')?.value || "0");
        setValue("stock", row.querySelector('[name="stock"]')?.value || "0");
        setValue("enabled", row.querySelector('[name="enabled"]')?.checked ? "true" : "false");
      }
      actionForm.requestSubmit();
    };
    editor.querySelectorAll("[data-lottery-award-row]").forEach((row) => {
      if (!(row instanceof HTMLElement)) return;
      row.querySelector("[data-lottery-award-update]")?.addEventListener("click", () => submitAction("update", row));
      row.querySelector("[data-lottery-award-delete]")?.addEventListener("click", () => {
        if (window.confirm("确定删除这个奖项吗？")) submitAction("delete", row);
      });
    });
    editor.querySelector("[data-lottery-award-add]")?.addEventListener("click", () => {
      const name = editor.querySelector("[data-lottery-add-name]")?.value?.trim() || "";
      if (!name) { window.alert("请填写奖项名称"); return; }
      setValue("operation", "add");
      setValue("award_id", "");
      setValue("name", name);
      setValue("probability", editor.querySelector("[data-lottery-add-probability]")?.value || "0");
      setValue("stock", editor.querySelector("[data-lottery-add-stock]")?.value || "0");
      setValue("enabled", editor.querySelector("[data-lottery-add-enabled]")?.checked ? "true" : "false");
      actionForm.requestSubmit();
    });
  });

  // Large groups can have several independent sign-in schemes and lottery
  // campaigns.  Keep the add form separate and reveal only the selected
  // record so an edit cannot be mistaken for another campaign.
  document.querySelectorAll("select[data-collection-picker]").forEach((picker) => {
    if (!(picker instanceof HTMLSelectElement)) return;
    const scope = picker.dataset.collectionPicker || "";
    const scopeNode = Array.from(document.querySelectorAll("[data-collection-scope]"))
      .find((node) => node.dataset.collectionScope === scope);
    const items = scopeNode
      ? Array.from(scopeNode.querySelectorAll("[data-collection-item]")).filter((item) => item instanceof HTMLElement)
      : [];
    const create = Array.from(document.querySelectorAll("[data-collection-create]"))
      .find((node) => node.dataset.collectionCreate === scope);
    const apply = () => {
      const selected = picker.value || "__new__";
      if (create instanceof HTMLElement) create.hidden = selected !== "__new__";
      items.forEach((item) => {
        item.hidden = selected === "__new__" || item.dataset.collectionId !== selected;
      });
    };
    picker.addEventListener("change", apply);
    apply();
  });

  // A refresh request is durable on Linux and fulfilled by the next outbound
  // Windows heartbeat.  Poll only the small read-only status endpoint while
  // that handshake is pending, so an open group page notices completion
  // without reloading (and therefore without discarding an unsaved draft).
  const syncStatusPanel = document.querySelector("[data-sync-status-url]");
  if (syncStatusPanel instanceof HTMLElement && syncStatusPanel.dataset.syncWatch === "1") {
    const statusUrl = syncStatusPanel.dataset.syncStatusUrl;
    const badgeNode = syncStatusPanel.querySelector("[data-sync-live-badge]");
    const detailNode = syncStatusPanel.querySelector("[data-sync-live-detail]");
    const sidebarBadgeNode = document.querySelector("[data-sync-sidebar-badge]");
    const sidebarTextNode = document.querySelector("[data-sync-sidebar-text]");
    const sidebarStatusNode = document.querySelector(".group-sidebar-status");
    const refreshButton = syncStatusPanel.querySelector("[data-sync-refresh-button]");
    const refreshForm = syncStatusPanel.querySelector("[data-sync-refresh-form]");
    const manualStatusRefresh = syncStatusPanel.querySelector("[data-sync-status-refresh]");
    let attempts = 0;
    let pollTimer = 0;
    let pollInFlight = false;
    const maxAttempts = 12; // at most two minutes in the foreground
    const foregroundInterval = 10000;
    const backgroundInterval = 30000;
    let settled = false;
    const setLiveStatus = (badge, detail, tone = "", sidebarText = detail) => {
      if (badgeNode) badgeNode.textContent = badge;
      if (detailNode) detailNode.textContent = detail;
      if (sidebarBadgeNode) sidebarBadgeNode.textContent = badge;
      if (sidebarTextNode) sidebarTextNode.textContent = sidebarText;
      if (sidebarStatusNode instanceof HTMLElement && tone) {
        sidebarStatusNode.classList.remove("is-online", "is-pending", "is-refresh", "is-error", "is-offline");
        sidebarStatusNode.classList.add(tone);
      }
      if (sidebarStatusNode instanceof HTMLElement) sidebarStatusNode.title = detail;
    };
    const syncTone = (sync) => {
      if (!sync || typeof sync !== "object") return "is-offline";
      if (["unavailable", "protected", "partial", "error"].includes(sync.status)) return "is-error";
      if (sync.pending) return "is-pending";
      if (sync.refresh_requested) return "is-refresh";
      return sync.online ? "is-online" : "is-offline";
    };
    const schedule = () => {
      if (attempts < maxAttempts && !pollTimer) {
        const delay = document.visibilityState === "hidden"
          ? backgroundInterval
          : foregroundInterval;
        pollTimer = window.setTimeout(() => {
          pollTimer = 0;
          poll();
        }, delay);
      }
    };
    const poll = async () => {
      if (pollInFlight) return;
      pollInFlight = true;
      attempts += 1;
      if (!statusUrl) {
        pollInFlight = false;
        return;
      }
      // A stalled reverse proxy/CDN must not leave the page permanently in
      // ``pollInFlight``.  Without a client-side deadline, one half-open
      // request made the “刷新状态” link appear unresponsive forever.
      const controller = typeof AbortController === "function"
        ? new AbortController()
        : null;
      const timeoutId = controller
        ? window.setTimeout(() => controller.abort(), 8000)
        : 0;
      try {
        const response = await fetch(statusUrl, {
          credentials: "same-origin",
          cache: "no-store",
          headers: { Accept: "application/json" },
          ...(controller ? { signal: controller.signal } : {}),
        });
        if (!response.ok) {
          setLiveStatus(
            "状态读取失败",
            `网页状态接口返回 HTTP ${response.status}；已保存的 Windows 同步请求不会丢失，请检查网站反代和服务日志。`,
            "is-error",
            `状态接口 HTTP ${response.status}`,
          );
          schedule();
          return;
        }
        const payload = await response.json();
        const sync = payload && typeof payload.sync === "object" ? payload.sync : null;
        if (!sync) {
          setLiveStatus(
            "状态数据异常",
            "网页没有返回可用的同步状态；已保存的 Windows 同步请求不会丢失，请稍后刷新状态。",
            "is-error",
            "状态接口返回异常",
          );
          schedule();
          return;
        }
        const badge = sync.badge || "同步状态未知";
        const lastSeen = sync.last_seen_text || "尚未收到 Windows 同步请求";
        const detail = `${lastSeen}。${sync.detail || sync.display_text || sync.text || ""}`;
        setLiveStatus(badge, detail, syncTone(sync), sync.display_text || sync.text || badge);
        if (refreshButton instanceof HTMLButtonElement) {
          refreshButton.disabled = Boolean(sync.refresh_requested);
        }
        // Keep watching every non-terminal state, including protected/partial
        // source reads and a retryable Windows apply error. Those states can
        // recover on a later heartbeat without a full page reload; stopping
        // the poll merely because the transport is online used to leave the
        // page showing a stale "waiting" diagnosis forever.
        settled = Boolean(
          sync.online && !sync.pending && !sync.refresh_requested && sync.status === "synced"
        );
        if (!settled) schedule();
      } catch (error) {
        // A temporary browser/CDN failure must not create another server
        // request storm; the next bounded poll or a manual refresh remains
        // available.
        if (error?.name === "AbortError") {
          setLiveStatus(
            "状态读取超时",
            "网页状态接口响应超过 8 秒；这不会取消 Windows 的同步请求，请检查域名、HTTPS 反代和服务器负载后重试。",
            "is-error",
            "状态接口响应超时",
          );
        } else {
          setLiveStatus(
            "状态读取失败",
            "浏览器暂时无法读取网页同步状态；这不会取消 Windows 的同步请求，请检查当前网络或稍后重试。",
            "is-error",
            "浏览器无法读取状态",
          );
        }
        schedule();
      } finally {
        if (timeoutId) window.clearTimeout(timeoutId);
        pollInFlight = false;
      }
    };
    if (manualStatusRefresh instanceof HTMLAnchorElement) {
      manualStatusRefresh.addEventListener("click", (event) => {
        // The link remains a useful no-JavaScript fallback, while an active
        // page performs an immediate read-only request instead of merely
        // jumping to the heading and making the user think the button did
        // nothing.
        event.preventDefault();
        if (pollInFlight) return;
        if (pollTimer) {
          window.clearTimeout(pollTimer);
          pollTimer = 0;
        }
        attempts = 0;
        poll();
      });
    }
    if (refreshForm instanceof HTMLFormElement) {
      refreshForm.addEventListener("submit", () => {
        // The request is intentionally queued for the next Windows heartbeat;
        // it cannot open a connection from Linux.  Update the live diagnostic
        // before the redirect so a slow reverse proxy still gives immediate
        // feedback instead of looking like a dead button.
        setLiveStatus(
          "请求已登记",
          "本群补传请求已保存，等待 Windows 插件主动连接网页后上传配置。",
          "is-refresh",
          "等待 Windows 主动同步",
        );
        if (refreshButton instanceof HTMLButtonElement) {
          refreshButton.disabled = true;
          refreshButton.textContent = "请求已提交…";
        }
      });
    }
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible" || settled || pollInFlight) return;
      if (pollTimer) {
        window.clearTimeout(pollTimer);
        pollTimer = 0;
      }
      // A tab may have been hidden during the whole retry window.  Give a
      // newly visible page one bounded read without creating a background
      // request loop.
      attempts = 0;
      poll();
    });
    window.setTimeout(poll, foregroundInterval);
  }

  // Keep long feature lists usable without another round trip.  Each saved
  // rule list gets an independent local search box; changing a filter never
  // changes the form draft or writes a new Windows revision.  Read-only
  // runtime tables use the same behavior, while the CMD table keeps its
  // server-side QQ/name/time filters and pager.
const initGroupCollectionSearch = () => {
  const memberMessageViewer = document.querySelector("[data-member-message-view]");
  const memberMessageTitle = memberMessageViewer?.querySelector("[data-member-message-title]");
  const memberMessageStatus = memberMessageViewer?.querySelector("[data-member-message-status]");
  const memberMessageBody = memberMessageViewer?.querySelector("[data-member-message-body]");
  let selectedMessageUser = "";
  let selectedMessageRecords = null;
  let selectedMessageRequest = 0;
  const messageTypeLabels = {
    face: "表情", image: "图片", record: "语音", audio: "语音", video: "视频",
    file: "文件", forward: "转发", node: "转发", at: "艾特", text: "文字", operation: "群管操作",
  };
  const messageText = (value, fallback = "-") => {
    const result = value == null ? "" : String(value);
    return result || fallback;
  };
  const isSafeMessageMediaUrl = (rawUrl) => {
    if (typeof rawUrl === "string" && /^data:image\/(?:png|jpeg|webp|gif|bmp);base64,[A-Za-z0-9+/=]+$/.test(rawUrl) && rawUrl.length <= 700000) return true;
    try {
      const url = new URL(String(rawUrl));
      const host = url.hostname.toLowerCase();
      return url.protocol === "https:" && ["qpic.cn", "gtimg.cn", "qq.com", "nt.qq.com.cn", "koishi.js.org"].some((suffix) => (
        host === suffix || host.endsWith(`.${suffix}`)
      ));
    } catch (_) {
      return false;
    }
  };
  const appendMessageVisuals = (container, item, limit = 6) => {
    if (!(container instanceof HTMLElement) || !item || typeof item !== "object") return;
    const declared = Array.isArray(item.media_items) ? item.media_items : [];
    const mediaItems = declared.length
      ? declared.slice(0, limit)
      : (Array.isArray(item.media_urls) ? item.media_urls.slice(0, limit).map((url) => ({ kind: "image", url })) : []);
    // Older Linux rows may contain only face_labels because they were stored
    // before face media was projected. Reconstruct ordinary QQ face images at
    // render time so historical messages do not stay text-only.
    const historicalFaces = Array.isArray(item.face_labels) ? item.face_labels : [];
    historicalFaces.forEach((rawFace) => {
      const faceId = String(rawFace || "").trim();
      if (!/^\d{1,4}$/.test(faceId)) return;
      const faceUrl = `https://koishi.js.org/QFace/assets/qq_emoji/${faceId}/png/${faceId}.png`;
      if (!mediaItems.some((media) => media && media.url === faceUrl) && mediaItems.length < limit) {
        mediaItems.push({ kind: "image", url: faceUrl, name: `QQ表情 ${faceId}` });
      }
    });
    mediaItems.forEach((rawItem) => {
      const rawUrl = rawItem && typeof rawItem === "object" ? rawItem.url : "";
      if (!isSafeMessageMediaUrl(rawUrl)) return;
      const kind = String(rawItem.kind || "file").toLowerCase();
      const title = String(rawItem.name || "").trim();
      const link = document.createElement("a");
      link.href = String(rawUrl);
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.className = `message-media-preview message-media-${kind}`;
      let preview = link;
      if (kind === "image") {
        link.setAttribute("aria-label", "打开群消息原图");
        const image = document.createElement("img");
        image.src = String(rawUrl);
        image.alt = title || "群消息图片";
        image.loading = "lazy";
        image.referrerPolicy = "no-referrer";
        image.addEventListener("error", () => link.remove());
        link.append(image);
      } else if (kind === "audio" || kind === "record") {
        const wrapper = document.createElement("span");
        wrapper.className = "message-media-preview message-media-audio";
        const label = document.createElement("span");
        label.className = "message-media-label";
        label.textContent = title ? `语音：${title}` : "语音（可播放）";
        const audio = document.createElement("audio");
        audio.controls = true;
        audio.preload = "metadata";
        audio.src = String(rawUrl);
        audio.addEventListener("error", () => {
          label.textContent = "该语音格式无法直接播放，点击下载或在新页面打开";
        });
        link.textContent = "打开原语音";
        link.setAttribute("aria-label", "打开或下载群语音");
        wrapper.append(label, audio, link);
        preview = wrapper;
      } else if (kind === "video") {
        const wrapper = document.createElement("span");
        wrapper.className = "message-media-preview message-media-video";
        const video = document.createElement("video");
        video.controls = true;
        video.preload = "metadata";
        video.src = String(rawUrl);
        video.addEventListener("error", () => wrapper.remove());
        link.textContent = "打开原视频";
        link.setAttribute("aria-label", "打开群视频原文件");
        wrapper.append(video, link);
        preview = wrapper;
      } else {
        link.classList.add("message-media-file");
        link.textContent = title ? `附件：${title}` : "打开群消息附件";
      }
      container.append(document.createElement("br"), preview);
    });
    const faceLabels = Array.isArray(item.face_labels) ? item.face_labels.slice(0, 8) : [];
    faceLabels.forEach((rawLabel) => {
      const label = String(rawLabel || "").replace(/^\/+/, "").trim();
      if (!label) return;
      const chip = document.createElement("span");
      chip.className = "message-face-label";
      chip.textContent = `QQ 表情：${label}`;
      container.append(document.createElement("br"), chip);
    });
  };
  const renderMemberMessages = () => {
    if (!(memberMessageBody instanceof HTMLElement) || !selectedMessageUser) return;
    const runtimeRecords = Array.isArray(window.__jokerRuntimeMessages) ? window.__jokerRuntimeMessages : [];
    const records = Array.isArray(selectedMessageRecords) ? selectedMessageRecords : runtimeRecords;
    const selected = records.filter((item) => item && String(item.user_id || "") === selectedMessageUser);
    if (memberMessageTitle instanceof HTMLElement) memberMessageTitle.textContent = `成员 ${selectedMessageUser} 的消息记录`;
    memberMessageBody.replaceChildren();
    if (!selected.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 5;
      cell.className = "muted";
      cell.textContent = records.length
        ? "该成员暂无已同步消息；只显示开启群消息记录后 Windows 收到的消息。"
        : "Windows 尚未同步消息记录，请确认群消息记录已开启并等待下一条群消息。";
      row.append(cell);
      memberMessageBody.append(row);
      if (memberMessageStatus instanceof HTMLElement) memberMessageStatus.textContent = cell.textContent;
      return;
    }
    if (memberMessageStatus instanceof HTMLElement) memberMessageStatus.textContent = `已同步 ${selected.length} 条记录，最新消息在前。`;
    if (Array.isArray(selectedMessageRecords) && memberMessageStatus instanceof HTMLElement) {
      memberMessageStatus.textContent = "已读取该成员 " + selected.length + " 条已保存消息，最新消息在前。";
    }
    selected.forEach((item) => {
      const row = document.createElement("tr");
      const time = document.createElement("td");
      time.textContent = messageText(item.timestamp_text || item.timestamp);
      const content = document.createElement("td");
      content.className = "breakable";
      content.textContent = messageText(item.content || (item.has_image ? "[图片]" : "-"));
      appendMessageVisuals(content, item, 4);
      const media = document.createElement("td");
      const kinds = String(item.message_types || "").split(",").map((kind) => kind.trim()).filter(Boolean);
      media.textContent = kinds.map((kind) => messageTypeLabels[kind] || kind).join("、") || "文字";
      const status = document.createElement("td");
      const direction = String(item.direction || "inbound").toLowerCase();
      const state = item.recalled ? "已撤回" : "正常";
      status.textContent = direction === "outbound" ? `机器人发送 · ${state}` : (direction === "system" ? `群管操作 · ${state}` : state);
      const recall = document.createElement("td");
      recall.className = "breakable";
      recall.textContent = item.recalled
        ? `${messageText(item.recall_reason, "系统撤回")}${item.recalled_at_text ? `\n${item.recalled_at_text}` : ""}`
        : "-";
      row.append(time, content, media, status, recall);
      memberMessageBody.append(row);
    });
  };
  document.addEventListener("joker-message-cache-updated", renderMemberMessages);
  const closeMemberMessageViewer = () => {
    selectedMessageUser = "";
    selectedMessageRecords = null;
    if (memberMessageViewer instanceof HTMLElement) memberMessageViewer.hidden = true;
  };
  memberMessageViewer?.querySelectorAll("[data-member-message-clear]").forEach((control) => {
    control.addEventListener("click", closeMemberMessageViewer);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && memberMessageViewer instanceof HTMLElement && !memberMessageViewer.hidden) {
      closeMemberMessageViewer();
    }
  });
  const loadMemberMessages = async (userId) => {
    const runtimePanel = document.querySelector("[data-runtime-status-url]");
    const runtimeUrl = runtimePanel instanceof HTMLElement
      ? String(runtimePanel.dataset.runtimeStatusUrl || "")
      : "";
    const baseEndpoint = runtimeUrl.replace(
      /\/runtime-status(?:\?.*)?$/,
      "/members/" + encodeURIComponent(userId) + "/messages",
    );
    if (!baseEndpoint) return;
    const requestId = ++selectedMessageRequest;
    if (memberMessageStatus instanceof HTMLElement) {
      memberMessageStatus.textContent = "正在读取该成员完整消息明细…";
    }
    try {
      let payload = null;
      let lastCount = -1;
      let stalledAttempts = 0;
      for (let attempt = 0; attempt < 16; attempt += 1) {
        const suffix = baseEndpoint.includes("?") ? "&" : "?";
        const response = await fetch(
          baseEndpoint + suffix + "sync=" + (attempt === 0 ? "1" : "0"),
          {
            headers: { Accept: "application/json" },
            credentials: "same-origin",
          },
        );
        payload = await response.json();
        if (requestId !== selectedMessageRequest || userId !== selectedMessageUser) return;
        if (!response.ok) {
          throw new Error(String(payload?.detail || "HTTP " + response.status));
        }
        selectedMessageRecords = Array.isArray(payload?.records) ? payload.records : [];
        renderMemberMessages();
        const expected = Number(payload?.expected_count || 0);
        if (!payload?.sync_pending || !Number.isFinite(expected) || selectedMessageRecords.length >= expected) {
          break;
        }
        stalledAttempts = selectedMessageRecords.length <= lastCount
          ? stalledAttempts + 1
          : 0;
        lastCount = selectedMessageRecords.length;
        if (memberMessageStatus instanceof HTMLElement) {
          memberMessageStatus.textContent = `正在从 Windows 本地缓存补传消息（${selectedMessageRecords.length}/${expected}）…`;
        }
        if (stalledAttempts >= 3) {
          if (memberMessageStatus instanceof HTMLElement) {
            memberMessageStatus.textContent = `Windows 尚未确认本次消息回补（当前 ${selectedMessageRecords.length}/${expected}）。已停止等待，Windows 终端将显示具体失败原因；修复后重新点击“查看消息”即可。`;
          }
          break;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 3000));
      }
    } catch (_) {
      if (requestId !== selectedMessageRequest || userId !== selectedMessageUser) return;
      selectedMessageRecords = null;
      renderMemberMessages();
      if (memberMessageStatus instanceof HTMLElement) {
        memberMessageStatus.textContent = "完整明细暂时读取失败，当前显示已轮询到的消息；请稍后再次点击查看。";
      }
    }
  };
  const groupMessagePanel = document.querySelector("[data-group-message-history-url]");
  const groupMessageList = groupMessagePanel?.querySelector("[data-group-message-list]");
  const groupMessageStatus = groupMessagePanel?.querySelector("[data-group-message-status]");
  const groupMessageSearch = groupMessagePanel?.querySelector("[data-group-message-search]");
  const groupMessageDirection = groupMessagePanel?.querySelector("[data-group-message-direction]");
  const groupMessageType = groupMessagePanel?.querySelector("[data-group-message-type]");
  const groupMessageState = groupMessagePanel?.querySelector("[data-group-message-state]");
  const groupMessageSort = groupMessagePanel?.querySelector("[data-group-message-sort]");
  let groupMessageRecords = [];
  let groupMessageLoading = false;
  const renderGroupMessageHistory = () => {
    if (!(groupMessageList instanceof HTMLElement)) return;
    const query = groupMessageSearch instanceof HTMLInputElement
      ? groupMessageSearch.value.trim().toLocaleLowerCase()
      : "";
    const wantedType = groupMessageType instanceof HTMLSelectElement ? groupMessageType.value : "";
    const wantedDirection = groupMessageDirection instanceof HTMLSelectElement ? groupMessageDirection.value : "";
    const wantedState = groupMessageState instanceof HTMLSelectElement ? groupMessageState.value : "";
    const ascending = groupMessageSort instanceof HTMLSelectElement && groupMessageSort.value === "asc";
    const records = groupMessageRecords.filter((item) => {
      if (!item || typeof item !== "object") return false;
      if (wantedDirection && String(item.direction || "inbound").toLowerCase() !== wantedDirection) return false;
      const kinds = String(item.message_types || "").split(",").map((kind) => kind.trim()).filter(Boolean);
      if (wantedType && !kinds.includes(wantedType)) return false;
      if (wantedState === "recalled" && !item.recalled) return false;
      if (wantedState === "normal" && item.recalled) return false;
      return !query || JSON.stringify(item).toLocaleLowerCase().includes(query);
    }).sort((left, right) => {
      const delta = Number(left.timestamp || 0) - Number(right.timestamp || 0);
      return ascending ? delta : -delta;
    });
    groupMessageList.replaceChildren();
    if (!records.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = groupMessageRecords.length
        ? "没有符合当前筛选条件的已保存消息。"
        : "暂时没有已保存的群消息；Windows 收到新消息后会先写入本地缓存，再静默上传。";
      groupMessageList.append(empty);
      return;
    }
    const fragment = document.createDocumentFragment();
    records.forEach((item) => {
      const row = document.createElement("article");
      row.className = `group-chat-message${item.recalled ? " is-recalled" : ""}`;
      const meta = document.createElement("div");
      meta.className = "group-chat-message-meta";
      const sender = document.createElement("strong");
      const direction = String(item.direction || "inbound").toLowerCase();
      sender.textContent = direction === "outbound"
        ? "机器人"
        : (direction === "system" ? "群管操作" : messageText(item.user_name || item.nickname || item.user_id, "未知成员"));
      const account = document.createElement("span");
      account.textContent = `${direction === "system" ? "系统" : `QQ ${messageText(item.user_id)}`} · ${messageText(item.timestamp_text || item.timestamp)}`;
      meta.append(sender, account);
      const bubble = document.createElement("div");
      bubble.className = "group-chat-message-bubble";
      bubble.textContent = messageText(item.content || (item.has_image ? "[图片]" : "-"));
      appendMessageVisuals(bubble, item, 6);
      const types = String(item.message_types || "").split(",").map((kind) => kind.trim()).filter(Boolean);
      const tags = document.createElement("div");
      tags.className = "group-chat-message-tags";
      const mediaTag = document.createElement("span");
      mediaTag.textContent = types.map((kind) => messageTypeLabels[kind] || kind).join("、") || "文字";
      tags.append(mediaTag);
      if (direction === "outbound") {
        const outbound = document.createElement("span");
        outbound.textContent = "机器人发送";
        tags.append(outbound);
      } else if (direction === "system") {
        const system = document.createElement("span");
        system.textContent = "群管操作";
        tags.append(system);
      }
      if (item.recalled) {
        const recalled = document.createElement("span");
        recalled.className = "is-danger";
        recalled.textContent = `已撤回${item.recall_reason ? `：${messageText(item.recall_reason)}` : ""}`;
        tags.append(recalled);
      }
      row.append(meta, bubble, tags);
      fragment.append(row);
    });
    groupMessageList.append(fragment);
  };
  const loadGroupMessageHistory = async () => {
    if (!(groupMessagePanel instanceof HTMLElement) || groupMessageLoading) return;
    const endpoint = String(groupMessagePanel.dataset.groupMessageHistoryUrl || "");
    if (!endpoint) return;
    groupMessageLoading = true;
    if (groupMessageStatus instanceof HTMLElement) groupMessageStatus.textContent = "正在读取 Linux 已保存聊天记录…";
    try {
      const response = await fetch(`${endpoint}?limit=10000`, {
        headers: { Accept: "application/json" }, credentials: "same-origin",
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(String(payload?.detail || `HTTP ${response.status}`));
      groupMessageRecords = Array.isArray(payload?.records) ? payload.records : [];
      renderGroupMessageHistory();
      if (groupMessageStatus instanceof HTMLElement) {
        groupMessageStatus.textContent = `已显示 ${groupMessageRecords.length} 条已保存群聊天记录；新消息会先写入 Windows，再按时间戳增量上传。`;
      }
    } catch (_) {
      if (groupMessageStatus instanceof HTMLElement) {
        groupMessageStatus.textContent = "聊天记录暂时读取失败；Windows 本地缓存不会被清空，请稍后点击刷新显示。";
      }
    } finally {
      groupMessageLoading = false;
    }
  };
  groupMessageSearch?.addEventListener("input", renderGroupMessageHistory);
  groupMessageDirection?.addEventListener("change", renderGroupMessageHistory);
  groupMessageType?.addEventListener("change", renderGroupMessageHistory);
  groupMessageState?.addEventListener("change", renderGroupMessageHistory);
  groupMessageSort?.addEventListener("change", renderGroupMessageHistory);
  groupMessagePanel?.querySelector("[data-group-message-reload]")?.addEventListener("click", loadGroupMessageHistory);
  if (groupMessagePanel instanceof HTMLElement) {
    void loadGroupMessageHistory();
    window.setInterval(() => {
      if (!groupMessagePanel.hidden && document.visibilityState === "visible") {
        void loadGroupMessageHistory();
      }
    }, 15_000);
  }
    const addSearch = (container, rows, labelText, emptyText) => {
      if (!(container instanceof HTMLElement) || !rows.length) return;
      const input = document.createElement("input");
      input.type = "search";
      input.className = "collection-search-input";
      input.placeholder = labelText || "搜索已保存内容";
      input.setAttribute("aria-label", input.placeholder);
      const label = document.createElement("label");
      label.className = "list-search";
      label.append(input);
      container.before(label);
      const pageSizeRaw = Number.parseInt(container.dataset.pageSize || "10", 10);
      let pageSize = Number.isFinite(pageSizeRaw) && pageSizeRaw > 0
        ? Math.min(100, pageSizeRaw)
        : 10;
      const pager = document.createElement("div");
      pager.className = "collection-pager pagination";
      pager.setAttribute("aria-label", "列表分页");
      const summary = document.createElement("span");
      summary.className = "pagination-summary";
      const links = document.createElement("div");
      links.className = "pagination-links";
      const pageSizeLabel = document.createElement("label");
      pageSizeLabel.className = "pagination-size";
      pageSizeLabel.textContent = "每页";
      const pageSizeSelect = document.createElement("select");
      pageSizeSelect.setAttribute("aria-label", "每页显示数量");
      [10, 20, 50, 100].forEach((size) => {
        const option = document.createElement("option");
        option.value = String(size);
        option.textContent = String(size);
        option.selected = size === pageSize;
        pageSizeSelect.append(option);
      });
      pageSizeSelect.addEventListener("change", () => {
        const next = Number.parseInt(pageSizeSelect.value, 10);
        if (!Number.isFinite(next) || next <= 0) return;
        pageSize = Math.min(100, next);
        currentPage = 1;
        renderPager();
      });
      pageSizeLabel.append(pageSizeSelect);
      pager.append(summary, pageSizeLabel, links);
      container.after(pager);
      let empty = null;
      let currentPage = 1;
      let matchedRows = rows.slice();
      const renderPager = () => {
        const pageCount = Math.max(1, Math.ceil(matchedRows.length / pageSize));
        currentPage = Math.min(Math.max(1, currentPage), pageCount);
        const start = (currentPage - 1) * pageSize;
        const end = Math.min(start + pageSize, matchedRows.length);
        rows.forEach((row) => { row.hidden = true; });
        matchedRows.slice(start, end).forEach((row) => { row.hidden = false; });
        summary.textContent = matchedRows.length
          ? `显示 ${start + 1}-${end} / 共 ${matchedRows.length} 条`
          : "共 0 条";
        links.replaceChildren();
        if (pageCount <= 1) {
          pager.hidden = !matchedRows.length;
          return;
        }
        pager.hidden = false;
        const addPageButton = (page, label, current = false) => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = `button ghost small${current ? " active" : ""}`;
          button.textContent = label;
          button.setAttribute("aria-label", `第 ${page} 页`);
          if (current) button.setAttribute("aria-current", "page");
          button.disabled = current;
          button.addEventListener("click", () => {
            currentPage = page;
            renderPager();
          });
          links.append(button);
        };
        if (currentPage > 1) addPageButton(currentPage - 1, "上一页");
        const first = Math.max(1, currentPage - 2);
        const last = Math.min(pageCount, currentPage + 2);
        if (first > 1) {
          addPageButton(1, "1", currentPage === 1);
          if (first > 2) {
            const ellipsis = document.createElement("span");
            ellipsis.className = "pagination-ellipsis";
            ellipsis.textContent = "…";
            links.append(ellipsis);
          }
        }
        for (let page = first; page <= last; page += 1) {
          if (page === 1 && first > 1) continue;
          addPageButton(page, String(page), page === currentPage);
        }
        if (last < pageCount) {
          if (last < pageCount - 1) {
            const ellipsis = document.createElement("span");
            ellipsis.className = "pagination-ellipsis";
            ellipsis.textContent = "…";
            links.append(ellipsis);
          }
          addPageButton(pageCount, String(pageCount), currentPage === pageCount);
        }
        if (currentPage < pageCount) addPageButton(currentPage + 1, "下一页");
      };
      const filter = () => {
        const needle = input.value.trim().toLocaleLowerCase();
        matchedRows = rows.filter((row) => (
          !needle || (row.textContent || "").toLocaleLowerCase().includes(needle)
        ));
        currentPage = 1;
        renderPager();
        if (!matchedRows.length && needle) {
          if (!empty) {
            empty = document.createElement("div");
            empty.className = "empty-state compact collection-no-match";
            empty.textContent = emptyText || "没有匹配内容";
            container.append(empty);
          }
          empty.hidden = false;
        } else if (empty) {
          empty.hidden = true;
        }
      };
      input.addEventListener("input", filter);
      renderPager();
    };

    document.querySelectorAll("[data-searchable-list]").forEach((list) => {
      if (!(list instanceof HTMLElement)) return;
      const rows = Array.from(list.children).filter((row) => (
        row instanceof HTMLElement && (
          row.dataset.collectionItem === "" ||
          row.dataset.collectionItem === "1" ||
          row.classList.contains("list-item") ||
          row.classList.contains("lottery-campaign-card")
        )
      ));
      addSearch(list, rows, list.dataset.searchPlaceholder, "没有匹配的已保存内容");
    });

    document.querySelectorAll(".group-section-panel .table-wrap:not(.console-table):not(.message-cache-table):not(.member-directory)").forEach((wrapper) => {
      if (!(wrapper instanceof HTMLElement)) return;
      const rows = Array.from(wrapper.querySelectorAll("tbody tr")).filter((row) => (
        row instanceof HTMLElement && !row.querySelector(".muted")
      ));
      addSearch(wrapper, rows, "搜索当前功能数据", "没有匹配的数据");
    });

    // Message history is intentionally not paged or truncated. Keep only a
    // local filter so the member-row "查看消息" shortcut still works.
    document.querySelectorAll(".message-cache-table").forEach((wrapper) => {
      if (!(wrapper instanceof HTMLElement)) return;
      const input = document.createElement("input");
      input.type = "search";
      input.className = "collection-search-input";
      input.dataset.messageSearch = "1";
      input.placeholder = "搜索消息、QQ号或媒体类型";
      input.setAttribute("aria-label", input.placeholder);
      const label = document.createElement("label");
      label.className = "list-search";
      label.append(input);
      wrapper.before(label);
      input.addEventListener("input", () => {
        wrapper.dispatchEvent(new CustomEvent("message-cache-filter", {
          detail: { query: input.value.trim() },
        }));
      });
    });

    const openMemberMessages = (userId) => {
      const normalizedUserId = String(userId || "").trim();
      if (!normalizedUserId) return;
      selectedMessageUser = normalizedUserId;
      selectedMessageRecords = null;
      window.__jokerSelectedMessageUser = normalizedUserId;
      if (memberMessageViewer instanceof HTMLElement) {
        memberMessageViewer.hidden = false;
        renderMemberMessages();
        memberMessageViewer.querySelector(".member-message-modal-dialog")?.focus();
      }
      void loadMemberMessages(normalizedUserId);
      const table = document.querySelector(".message-cache-table");
      if (!(table instanceof HTMLElement)) return;
      // The table starts as an empty stub until Windows sends the first
      // heartbeat. Filter dynamic rows directly so this shortcut still works
      // when the search control did not exist at page load.
      table.querySelectorAll("tbody tr[data-message-user-id]").forEach((row) => {
        if (row instanceof HTMLElement) {
          row.hidden = String(row.dataset.messageUserId || "") !== normalizedUserId;
        }
      });
      const messageSearch = table.previousElementSibling?.querySelector("input[data-message-search]");
      if (messageSearch instanceof HTMLInputElement) {
        messageSearch.value = normalizedUserId;
        messageSearch.dispatchEvent(new Event("input", { bubbles: true }));
      }
      table.closest(".runtime-panel-block")?.scrollIntoView({ behavior: "smooth", block: "start" });
    };

    document.querySelectorAll("[data-member-directory-toolbar]").forEach((toolbar) => {
      if (!(toolbar instanceof HTMLElement)) return;
      const panel = toolbar.closest("[data-group-section-panel]");
      const directory = panel?.querySelector(".member-directory");
      const body = directory?.querySelector("tbody");
      const dataNode = panel?.querySelector("[data-member-directory-data]");
      const pager = panel?.querySelector("[data-member-directory-pager]");
      const selectorInput = panel?.querySelector("[data-member-filter]");
      const selector = panel?.querySelector("[data-member-select]");
      if (!(body instanceof HTMLTableSectionElement) || !(dataNode instanceof HTMLScriptElement)) return;

      let rawMembers = [];
      try {
        const parsed = JSON.parse(dataNode.textContent || "[]");
        rawMembers = Array.isArray(parsed) ? parsed : [];
      } catch (_) {
        rawMembers = [];
      }
      const number = (value) => {
        const parsed = Number(value || 0);
        return Number.isFinite(parsed) ? parsed : 0;
      };
      const roleLabel = (member) => ({ owner: "群主", admin: "管理员" })[String(member.role || "")] || "成员";
      const memberName = (member) => String(
        member.nickname || member.card || member.user_name || "未获取姓名"
      );
      let members = rawMembers.filter((member) => member && typeof member === "object").map((member, index) => {
        const searchable = [
          member.user_id, member.nickname, member.user_name, member.card, member.title,
          member.role, member.level_text, member.qq_level, member.group_level,
          member.activity_count, roleLabel(member),
        ].join(" ").toLocaleLowerCase();
        return { ...member, _index: index, _searchable: searchable };
      });
      const search = toolbar.querySelector("[data-member-directory-search]");
      const filter = toolbar.querySelector("[data-member-directory-filter]");
      const sort = toolbar.querySelector("[data-member-directory-sort]");
      let pageSize = Math.max(1, Math.min(100, number(directory?.dataset.pageSize) || 20));
      let currentPage = 1;
      let renderScheduled = false;
      let directoryLoaded = false;
      let directoryLoading = false;

      const memberSelectLabel = (member) => {
        const card = member.card ? ` · 群名片：${member.card}` : "";
        return `${memberName(member)}（QQ：${member.user_id || "-"}）${card} · QQ等级：${member.level_text || member.qq_level || "未返回"} · 群等级：${number(member.group_level)} · 近30天：${number(member.activity_count)}条 · ${roleLabel(member)}`;
      };
      const refreshMemberSelector = () => {
        if (!(selector instanceof HTMLSelectElement)) return;
        const selected = new Set(Array.from(selector.selectedOptions).map((option) => String(option.value || "")).filter(Boolean));
        const needle = selectorInput instanceof HTMLInputElement
          ? selectorInput.value.trim().toLocaleLowerCase()
          : "";
        const matched = needle
          ? members.filter((member) => member._searchable.includes(needle))
          : members;
        const visible = matched.slice(0, 100);
        selector.replaceChildren();
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.disabled = true;
        placeholder.textContent = !members.length
          ? "等待 Windows 同步成员"
          : needle && !matched.length
            ? "没有匹配成员"
            : matched.length > visible.length
              ? `匹配 ${matched.length} 人，请继续输入缩小范围`
              : `请选择成员（匹配 ${matched.length} 人）`;
        selector.append(placeholder);
        const appendOption = (member) => {
          const option = document.createElement("option");
          option.value = String(member.user_id || "");
          option.textContent = memberSelectLabel(member);
          option.selected = selected.has(option.value);
          selector.append(option);
        };
        visible.forEach(appendOption);
        selected.forEach((selectedId) => {
          const selectedMember = members.find((member) => String(member.user_id || "") === selectedId);
          if (selectedMember && !visible.some((member) => String(member.user_id || "") === selectedId)) appendOption(selectedMember);
        });
      };
      const appendCell = (row, value, className = "") => {
        const cell = document.createElement("td");
        if (className) cell.className = className;
        cell.textContent = value == null || value === "" ? "-" : String(value);
        row.append(cell);
        return cell;
      };
      const createRow = (member) => {
        const row = document.createElement("tr");
        row.dataset.memberRow = "1";
        row.dataset.activity = String(number(member.activity_count));
        row.dataset.lastMessage = String(number(member.last_message_at));
        row.dataset.qqLevel = String(number(member.qq_level));
        row.dataset.muted = member.mute_until_text && member.mute_until_text !== "未禁言" ? "1" : "0";
        row.dataset.role = String(member.role || "member");
        appendCell(row, member.user_id);
        appendCell(row, member.nickname);
        appendCell(row, member.card);
        appendCell(row, roleLabel(member));
        appendCell(row, member.level_text || member.qq_level || "未返回");
        appendCell(row, number(member.group_level));
        appendCell(row, member.title);
        appendCell(row, member.join_time_text);
        const activity = appendCell(row, "");
        activity.textContent = `${number(member.activity_count)} 条 / 30天`;
        activity.append(document.createElement("br"));
        const detail = document.createElement("small");
        detail.textContent = `文字 ${number(member.text_count)} · 图片 ${number(member.image_count)} · 视频 ${number(member.video_count)} · 文件 ${number(member.file_count)} · 转发 ${number(member.forward_count)}`;
        activity.append(detail);
        appendCell(row, member.last_message_text || member.last_message_types || "暂无消息内容", "breakable");
        appendCell(row, member.last_message_at ? member.last_message_at_text || "-" : member.last_sent_time_text || "暂无发言记录");
        appendCell(row, member.mute_until_text || "未禁言");
        const action = document.createElement("td");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "button ghost small";
        button.dataset.messageViewUser = String(member.user_id || "");
        button.textContent = "查看消息";
        action.append(button);
        row.append(action);
        return row;
      };
      const renderPager = (matchedCount, pageCount, start, end) => {
        if (!(pager instanceof HTMLElement)) return;
        pager.replaceChildren();
        pager.hidden = !matchedCount;
        const summary = document.createElement("span");
        summary.className = "pagination-summary";
        summary.textContent = `显示 ${start + 1}-${end} / 共 ${matchedCount} 人`;
        const sizeLabel = document.createElement("label");
        sizeLabel.className = "pagination-size";
        sizeLabel.textContent = "每页";
        const sizeSelect = document.createElement("select");
        sizeSelect.setAttribute("aria-label", "成员每页显示数量");
        [10, 20, 50, 100].forEach((size) => {
          const option = document.createElement("option");
          option.value = String(size);
          option.textContent = `${size} 人`;
          option.selected = size === pageSize;
          sizeSelect.append(option);
        });
        sizeSelect.addEventListener("change", () => {
          const next = Number.parseInt(sizeSelect.value, 10);
          if (!Number.isFinite(next) || next < 1) return;
          pageSize = Math.min(100, Math.max(1, next));
          currentPage = 1;
          render();
        });
        sizeLabel.append(sizeSelect);
        pager.append(summary, sizeLabel);
        const jumpLabel = document.createElement("label");
        jumpLabel.className = "pagination-size";
        jumpLabel.textContent = "跳转到";
        const jumpInput = document.createElement("input");
        jumpInput.type = "number";
        jumpInput.min = "1";
        jumpInput.max = String(pageCount);
        jumpInput.value = String(currentPage);
        jumpInput.inputMode = "numeric";
        jumpInput.setAttribute("aria-label", "成员页码");
        jumpInput.addEventListener("change", () => {
          const next = Number.parseInt(jumpInput.value, 10);
          if (!Number.isFinite(next)) {
            jumpInput.value = String(currentPage);
            return;
          }
          currentPage = Math.min(pageCount, Math.max(1, next));
          render();
        });
        jumpLabel.append(jumpInput, document.createTextNode(` / ${pageCount} 页`));
        pager.append(jumpLabel);
        if (pageCount <= 1) return;
        const links = document.createElement("div");
        links.className = "pagination-links";
        const addButton = (page, label, active = false) => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = `button ghost small${active ? " active" : ""}`;
          button.textContent = label;
          button.disabled = active;
          button.addEventListener("click", () => { currentPage = page; render(); });
          links.append(button);
        };
        if (currentPage > 1) addButton(currentPage - 1, "上一页");
        const first = Math.max(1, currentPage - 2);
        const last = Math.min(pageCount, currentPage + 2);
        for (let page = first; page <= last; page += 1) addButton(page, String(page), page === currentPage);
        if (currentPage < pageCount) addButton(currentPage + 1, "下一页");
        pager.append(links);
      };
      const render = () => {
        const query = search instanceof HTMLInputElement ? search.value.trim().toLocaleLowerCase() : "";
        const mode = filter instanceof HTMLSelectElement ? filter.value : "all";
        const sortMode = sort instanceof HTMLSelectElement ? sort.value : "activity_desc";
        const matched = members.filter((member) => {
          if (query && !member._searchable.includes(query)) return false;
          if (mode === "active") return number(member.activity_count) > 0;
          if (mode === "silent") return number(member.activity_count) === 0;
          if (mode === "muted") return Boolean(member.mute_until_text && member.mute_until_text !== "未禁言");
          if (mode === "admin") return ["owner", "admin"].includes(String(member.role || ""));
          return true;
        });
        const ordering = {
          activity_desc: ["activity_count", -1], activity_asc: ["activity_count", 1],
          last_desc: ["last_message_at", -1], last_asc: ["last_message_at", 1],
          qq_level_desc: ["qq_level", -1], qq_level_asc: ["qq_level", 1],
        };
        const [key, direction] = ordering[sortMode] || ordering.activity_desc;
        matched.sort((left, right) => {
          const delta = number(left[key]) - number(right[key]);
          if (delta) return delta * direction;
          return memberName(left).localeCompare(memberName(right), "zh-CN") || left._index - right._index;
        });
        const pageCount = Math.max(1, Math.ceil(matched.length / pageSize));
        currentPage = Math.min(Math.max(1, currentPage), pageCount);
        const start = (currentPage - 1) * pageSize;
        const visible = matched.slice(start, start + pageSize);
        body.replaceChildren();
        if (!visible.length) {
          const row = document.createElement("tr");
          const cell = document.createElement("td");
          cell.colSpan = 13;
          cell.className = "muted";
          cell.textContent = members.length ? "没有符合当前筛选条件的成员。" : "暂未收到成员列表。";
          row.append(cell);
          body.append(row);
        } else {
          const fragment = document.createDocumentFragment();
          visible.forEach((member) => fragment.append(createRow(member)));
          body.append(fragment);
        }
        renderPager(matched.length, pageCount, start, Math.min(start + visible.length, matched.length));
      };
      const scheduleRender = (resetPage = false) => {
        if (resetPage) currentPage = 1;
        if (renderScheduled) return;
        renderScheduled = true;
        window.requestAnimationFrame(() => {
          renderScheduled = false;
          render();
        });
      };
      const loadDirectory = async (force = false) => {
        if ((!force && directoryLoaded) || directoryLoading) return;
        const endpoint = String(panel?.dataset.memberDirectoryUrl || "");
        if (!endpoint) return;
        directoryLoading = true;
        const hadDirectory = directoryLoaded;
        if (!hadDirectory) {
          body.replaceChildren();
          const loadingRow = document.createElement("tr");
          const loadingCell = document.createElement("td");
          loadingCell.colSpan = 13;
          loadingCell.className = "muted";
          loadingCell.textContent = "正在读取 Linux 网页成员目录…";
          loadingRow.append(loadingCell);
          body.append(loadingRow);
        }
        try {
          const response = await fetch(endpoint, {
            credentials: "same-origin",
            cache: "no-store",
            headers: { Accept: "application/json" },
          });
          const payload = await response.json();
          if (!response.ok) throw new Error(String(payload?.detail || `HTTP ${response.status}`));
          const incoming = payload?.members?.members;
          rawMembers = Array.isArray(incoming) ? incoming : [];
          members = rawMembers.filter((member) => member && typeof member === "object").map((member, index) => {
            const searchable = [
              member.user_id, member.nickname, member.user_name, member.card, member.title,
              member.role, member.level_text, member.qq_level, member.group_level,
              member.activity_count, roleLabel(member),
            ].join(" ").toLocaleLowerCase();
            return { ...member, _index: index, _searchable: searchable };
          });
          directoryLoaded = true;
          refreshMemberSelector();
          refreshMemberReplyOptions();
          scheduleRender(true);
        } catch (_) {
          if (!hadDirectory) {
            body.replaceChildren();
            const errorRow = document.createElement("tr");
            const errorCell = document.createElement("td");
            errorCell.colSpan = 13;
            errorCell.className = "muted";
            errorCell.textContent = "成员目录暂时读取失败，请稍后重新打开成员管理。";
            errorRow.append(errorCell);
            body.append(errorRow);
          }
        } finally {
          directoryLoading = false;
        }
      };
      search?.addEventListener("input", () => scheduleRender(true));
      filter?.addEventListener("change", () => scheduleRender(true));
      sort?.addEventListener("change", () => scheduleRender(true));
      selectorInput?.addEventListener("input", refreshMemberSelector);
      selector?.addEventListener("focus", refreshMemberSelector);
      // Keep the open page converged with Windows joins/leaves, name changes,
      // mute state and message activity without forcing a full page reload.
      window.setInterval(() => {
        if (document.visibilityState === "visible" && directoryLoaded) loadDirectory(true);
      }, 10000);
      const memberReplyOptions = document.querySelector("#group-member-options");
      const refreshMemberReplyOptions = (input) => {
        if (!(memberReplyOptions instanceof HTMLDataListElement)) return;
        const needle = input instanceof HTMLInputElement
          ? input.value.trim().toLocaleLowerCase()
          : "";
        const matched = needle
          ? members.filter((member) => member._searchable.includes(needle))
          : members;
        memberReplyOptions.replaceChildren();
        matched.slice(0, 100).forEach((member) => {
          const option = document.createElement("option");
          option.value = String(member.user_id || "");
          option.label = `${memberName(member)}${member.card ? ` · ${member.card}` : ""}`;
          memberReplyOptions.append(option);
        });
      };
      document.querySelectorAll("input[list='group-member-options']").forEach((input) => {
        if (!(input instanceof HTMLInputElement)) return;
        input.addEventListener("input", () => refreshMemberReplyOptions(input));
        input.addEventListener("focus", () => refreshMemberReplyOptions(input));
      });
      refreshMemberSelector();
      render();
      document.querySelectorAll("[data-group-section-switch='member-management']").forEach((link) => {
        link.addEventListener("click", () => { void loadDirectory(); });
      });
      if (!(panel instanceof HTMLElement) || !panel.hidden) void loadDirectory();
    });

    // Member rows are paged and created after this script starts, so bind
    // through the document instead of attaching a listener to every row.
    document.addEventListener("click", (event) => {
      if (!(event.target instanceof Element)) return;
      const button = event.target.closest("[data-message-view-user]");
      if (!(button instanceof HTMLButtonElement)) return;
      openMemberMessages(button.dataset.messageViewUser);
    });
  };
  initGroupCollectionSearch();

  // Runtime data is read independently from the configuration page.  The
  // browser asks for a version marker first; unchanged heartbeats transfer no
  // member directory or message history and never reload the document.
  const initRuntimeStatus = () => {
    const panel = document.querySelector("[data-runtime-status-url]");
    if (!(panel instanceof HTMLElement)) return;
    const statusUrl = panel.dataset.runtimeStatusUrl || "";
    if (!statusUrl) return;
    const messageTable = panel.querySelector(".message-cache-table");
    const messageBody = messageTable?.querySelector("tbody");
    const messagePager = document.createElement("div");
    messagePager.className = "collection-pager pagination message-cache-pager";
    messagePager.setAttribute("aria-label", "消息记录分页");
    messageTable?.after(messagePager);
    const countNode = panel.querySelector(".runtime-message-count");
    const badge = panel.querySelector("[data-runtime-live-badge]");
    const operationBody = panel.querySelector("[data-runtime-operation-list]");
    const operationCategory = panel.querySelector("[data-operation-category]");
    const operationStatus = panel.querySelector("[data-operation-status]");
    let version = "";
    let inFlight = false;
    let allMessages = [];
    let messagePage = 1;
    const messagePageSize = 50;
    const typeLabels = {
      face: "表情", image: "图片", record: "语音", audio: "语音", video: "视频",
      file: "文件", forward: "转发", node: "转发", at: "艾特", text: "文字", operation: "群管操作",
    };
    const text = (value, fallback = "-") => {
      const result = value == null ? "" : String(value);
      return result || fallback;
    };
    const appendCell = (row, value, className = "") => {
      const cell = document.createElement("td");
      if (className) cell.className = className;
      cell.textContent = text(value);
      row.append(cell);
    };
    const renderOperations = (operations) => {
      if (!(operationBody instanceof HTMLElement)) return;
      const wanted = operationCategory instanceof HTMLSelectElement ? operationCategory.value : "";
      const records = Array.isArray(operations?.recent) ? operations.recent : [];
      const visible = records.filter((item) => !wanted || String(item?.category || "其他") === wanted);
      operationBody.replaceChildren();
      if (!visible.length) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 6;
        cell.className = "muted";
        cell.textContent = records.length ? "没有符合当前功能分类的群管操作。" : "暂无群管操作记录";
        row.append(cell);
        operationBody.append(row);
      } else {
        const fragment = document.createDocumentFragment();
        visible.slice(0, 100).forEach((item) => {
          if (!item || typeof item !== "object") return;
          const row = document.createElement("tr");
          appendCell(row, item.timestamp_text || item.timestamp);
          appendCell(row, item.category || "其他");
          appendCell(row, item.operator_name || item.operator_id);
          appendCell(row, item.action);
          appendCell(row, item.target_name || item.target_id);
          appendCell(row, item.reason, "breakable");
          fragment.append(row);
        });
        operationBody.append(fragment);
      }
      if (operationStatus instanceof HTMLElement) {
        operationStatus.textContent = `已同步 ${records.length} 条群管操作${wanted ? `；当前：${wanted}` : ""}`;
      }
    };
    const renderMessages = (records) => {
      if (!(messageBody instanceof HTMLElement)) return;
      allMessages = Array.isArray(records) ? records : [];
      window.__jokerRuntimeMessages = allMessages;
      document.dispatchEvent(new CustomEvent("joker-message-cache-updated"));
      messagePage = 1;
      renderMessagePage();
    };
    const renderMessagePage = () => {
      if (!(messageBody instanceof HTMLElement)) return;
      const search = messageTable?.previousElementSibling?.querySelector("input[data-message-search]");
      const needle = search instanceof HTMLInputElement ? search.value.trim().toLocaleLowerCase() : "";
      const filtered = needle
        ? allMessages.filter((item) => JSON.stringify(item).toLocaleLowerCase().includes(needle))
        : allMessages;
      const pageCount = Math.max(1, Math.ceil(filtered.length / messagePageSize));
      messagePage = Math.min(Math.max(1, messagePage), pageCount);
      const visible = filtered.slice((messagePage - 1) * messagePageSize, messagePage * messagePageSize);
      messageBody.replaceChildren();
      if (!filtered.length) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 7;
        cell.className = "muted";
        cell.textContent = "暂无消息缓存。Windows 开启群消息记录后，新消息会自动出现。";
        row.append(cell);
        messageBody.append(row);
        if (countNode instanceof HTMLElement) countNode.textContent = String(allMessages.length);
        messagePager.replaceChildren();
        return;
      }
      const fragment = document.createDocumentFragment();
      visible.forEach((item) => {
        if (!item || typeof item !== "object") return;
        const row = document.createElement("tr");
        row.dataset.messageUserId = text(item.user_id, "");
        row.dataset.messageTypes = text(item.message_types, "");
        appendCell(row, item.timestamp_text || item.timestamp);
        appendCell(row, item.user_id);
        appendCell(row, item.user_name || item.nickname);
        appendCell(row, item.content || (item.has_image ? "[图片]" : "-"), "breakable");
        const contentCell = row.cells[3];
        appendMessageVisuals(contentCell, item, 4);
        const kinds = String(item.message_types || "").split(",").map((kind) => kind.trim()).filter(Boolean);
        const media = kinds.map((kind) => typeLabels[kind] || kind).join("、") || "文字";
        const counts = [["image", "图片"], ["record", "语音"], ["video", "视频"], ["file", "文件"], ["forward", "转发"]]
          .map(([key, label]) => item[`${key}_count`] ? `${label} ${item[`${key}_count`]}` : "")
          .filter(Boolean);
        appendCell(row, [media, ...counts].join(" · "));
        appendCell(row, item.recalled ? "已撤回" : "正常");
        const recall = item.recalled
          ? `${text(item.recall_reason, "系统撤回")}${item.recalled_at_text ? `\n${item.recalled_at_text}` : ""}`
          : "-";
        appendCell(row, recall, "breakable");
        fragment.append(row);
      });
      messageBody.append(fragment);
      if (countNode instanceof HTMLElement) countNode.textContent = String(allMessages.length);
      messagePager.replaceChildren();
      const summary = document.createElement("span");
      summary.className = "pagination-summary";
      const start = (messagePage - 1) * messagePageSize + 1;
      summary.textContent = `显示 ${start}-${Math.min(start + visible.length - 1, filtered.length)} / 匹配 ${filtered.length} 条`;
      const previous = document.createElement("button");
      previous.type = "button";
      previous.className = "button ghost small";
      previous.textContent = "上一页";
      previous.disabled = messagePage <= 1;
      previous.addEventListener("click", () => { messagePage -= 1; renderMessagePage(); });
      const next = document.createElement("button");
      next.type = "button";
      next.className = "button ghost small";
      next.textContent = "下一页";
      next.disabled = messagePage >= pageCount;
      next.addEventListener("click", () => { messagePage += 1; renderMessagePage(); });
      const page = document.createElement("span");
      page.className = "pagination-summary";
      page.textContent = `${messagePage} / ${pageCount}`;
      messagePager.append(summary, previous, page, next);
      messagePager.hidden = filtered.length <= messagePageSize;
    };
    messageTable?.addEventListener("message-cache-filter", () => {
      messagePage = 1;
      renderMessagePage();
    });
    operationCategory?.addEventListener("change", () => {
      renderOperations(window.__jokerRuntimeOperations || {});
    });
    const poll = async () => {
      if (inFlight || document.visibilityState === "hidden") return;
      inFlight = true;
      try {
        const separator = statusUrl.includes("?") ? "&" : "?";
        const response = await fetch(`${statusUrl}${separator}version=${encodeURIComponent(version)}`, {
          credentials: "same-origin",
          cache: "no-store",
          headers: { Accept: "application/json" },
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        if (payload.version) version = String(payload.version);
        if (!payload.unchanged && payload.runtime) {
          renderMessages(payload.runtime.message_cache?.records || []);
          window.__jokerRuntimeOperations = payload.runtime.operations || {};
          renderOperations(window.__jokerRuntimeOperations);
          if (badge instanceof HTMLElement) {
            badge.textContent = "已自动同步";
            badge.classList.remove("is-error");
          }
        }
      } catch (error) {
        if (badge instanceof HTMLElement) {
          badge.textContent = "同步重试中";
          badge.title = error?.message || "运行数据接口暂时不可用";
          badge.classList.add("is-error");
        }
      } finally {
        inFlight = false;
      }
    };
    poll();
    window.setInterval(poll, 5000);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") poll();
    });
  };
  initRuntimeStatus();

  // Do not reload after a member refresh. A reload used to discard unsaved
  // edits in other feature panels while Windows was still fetching members.

  // A whole-group mute/unmute task has no message body and cannot @ members.
  // Keep that distinction visible before submission so a customer does not
  // accidentally assume an ignored text field will be sent by the Windows bot.
  const refreshTimedTaskForm = (form) => {
    const action = form.querySelector("[data-timed-action]");
    const messageRow = form.querySelector("[data-timed-message]");
    const message = messageRow?.querySelector("textarea[name='message']");
    const messageLabel = form.querySelector("[data-timed-message-label]");
    const messageHelp = form.querySelector("[data-timed-message-help]");
    const atAllRow = form.querySelector("[data-timed-at-all]");
    const atAll = atAllRow?.querySelector("input[name='at_all']");
    const isMessage = action?.value === "message";
    const isMusic = action?.value === "random_music";
    const isHourly = action?.value === "hourly_time";
    const canAtAll = isMessage || isHourly;
    const hasContent = isMessage || isMusic || isHourly;
    if (messageRow) messageRow.hidden = !hasContent;
    // Hourly reports generate their own text; an optional suffix must not
    // prevent the form from being submitted when it is left blank.
    if (message) message.required = isMessage;
    if (message instanceof HTMLTextAreaElement) {
      message.placeholder = isMusic
        ? "可填写歌名或歌手；留空则每次自动随机播放一首"
        : isHourly
          ? "可选，例如：请合理安排时间。"
          : "例如：早上好，今天也请遵守群规。";
    }
    if (messageLabel) messageLabel.textContent = isMusic ? "随机歌名或歌手" : (isHourly ? "报时后缀（可选）" : "播报内容");
    if (messageHelp) messageHelp.textContent = isMusic
      ? "可用逗号、竖线或换行填写多个歌名/歌手；留空也可以保存，执行时只随机播放一首。"
      : isHourly
        ? "机器人会自动生成“现在是 HH:00，整点报时。”，这里的内容会追加到末尾。"
        : "全体禁言/解禁任务不需要填写内容。";
    if (atAllRow) atAllRow.hidden = !canAtAll;
    if (atAll) {
      atAll.disabled = !canAtAll;
      // Do not overwrite a saved value when an administrator only changes a
      // different field. Unsupported task types simply do not submit @all.
    }
  };

  document.querySelectorAll("[data-timed-task-form]").forEach((form) => {
    refreshTimedTaskForm(form);
    const hourly = form.querySelector("[data-hourly-toggle]");
    const cron = form.querySelector("input[name='cron_expr']");
    const runAt = form.querySelector("input[name='run_at']");
    const name = form.querySelector("input[name='name']");
    hourly?.addEventListener("change", () => {
      if (!(cron instanceof HTMLInputElement)) return;
      if (hourly.checked) {
        cron.value = "0 * * * *";
        if (runAt instanceof HTMLInputElement) runAt.value = "";
        if (name instanceof HTMLInputElement && !name.value.trim()) name.value = "整点播报";
      }
    });
    runAt?.addEventListener("change", () => {
      if (!(runAt instanceof HTMLInputElement) || !runAt.value) return;
      if (cron instanceof HTMLInputElement) cron.value = "";
      if (hourly instanceof HTMLInputElement) hourly.checked = false;
      if (name instanceof HTMLInputElement && !name.value.trim()) name.value = "一次性播报";
    });
    form.addEventListener("reset", () => {
      // HTMLFormElement.reset() restores the action select synchronously, but
      // it does not rerun the visibility/required rules above.
      window.setTimeout(() => refreshTimedTaskForm(form), 0);
    });
    form.querySelector("[data-timed-action]")?.addEventListener("change", () => {
      refreshTimedTaskForm(form);
    });
  });

  // Timed-task editing used to open an absolutely positioned panel inside a
  // task row.  On wide pages that panel overlapped unrelated tasks and the
  // footer.  Keep edits in a focused modal instead.
  const timedTaskEditors = Array.from(document.querySelectorAll("[data-timed-task-editor]"));
  const timedTaskEditorOpeners = Array.from(document.querySelectorAll("[data-timed-task-editor-open]"));
  const findTimedTaskEditor = (taskId) => timedTaskEditors.find(
    (item) => item.getAttribute("data-timed-task-editor") === taskId,
  );
  const findTimedTaskEditorOpener = (taskId) => timedTaskEditorOpeners.find(
    (item) => item.getAttribute("data-timed-task-editor-open") === taskId,
  );
  const closeTimedTaskEditor = (editor, opener = null) => {
    if (!(editor instanceof HTMLElement)) return;
    editor.hidden = true;
    if (opener instanceof HTMLElement) opener.focus();
  };
  timedTaskEditorOpeners.forEach((button) => {
    button.addEventListener("click", () => {
      const taskId = button.getAttribute("data-timed-task-editor-open") || "";
      const editor = findTimedTaskEditor(taskId);
      if (!(editor instanceof HTMLElement)) return;
      timedTaskEditors.forEach((item) => { if (item !== editor) item.hidden = true; });
      editor.hidden = false;
      window.setTimeout(() => editor.querySelector(".timed-task-modal-dialog")?.focus(), 0);
    });
  });
  document.querySelectorAll("[data-timed-task-editor-close]").forEach((button) => {
    button.addEventListener("click", () => {
      const editor = button.closest("[data-timed-task-editor]");
      const taskId = editor?.getAttribute("data-timed-task-editor") || "";
      const opener = findTimedTaskEditorOpener(taskId);
      closeTimedTaskEditor(editor, opener);
    });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const editor = timedTaskEditors.find((item) => !item.hidden);
    if (!editor) return;
    const taskId = editor.getAttribute("data-timed-task-editor") || "";
    closeTimedTaskEditor(editor, findTimedTaskEditorOpener(taskId));
  });

  // Preview reply images only in the browser before a form is sent.  The
  // selected file is still validated and re-encoded by Linux; object URLs are
  // never persisted in a rule or synchronized to Windows.
  document.querySelectorAll("[data-reply-form]").forEach((replyForm) => {
    if (!(replyForm instanceof HTMLFormElement)) return;
    const imageInput = replyForm.querySelector("[data-reply-images]");
    const preview = replyForm.querySelector("[data-reply-image-preview]");
    const submitButton = replyForm.querySelector("[data-reply-submit]");
    const defaultSubmitLabel = submitButton instanceof HTMLButtonElement
      ? (submitButton.textContent || "保存本条")
      : "保存本条";
    let previewUrls = [];
    const clearPreview = () => {
      previewUrls.forEach((url) => URL.revokeObjectURL(url));
      previewUrls = [];
      if (preview instanceof HTMLElement) preview.replaceChildren();
    };
    if (imageInput instanceof HTMLInputElement && preview instanceof HTMLElement) {
      imageInput.addEventListener("change", () => {
        clearPreview();
        const selectedFiles = Array.from(imageInput.files || []);
        if (selectedFiles.length > 20) {
          imageInput.setCustomValidity("每条自动回复最多上传 20 张图片，请重新选择。");
          imageInput.reportValidity();
        } else {
          imageInput.setCustomValidity("");
        }
        selectedFiles.slice(0, 20).forEach((file) => {
          const row = document.createElement("div");
          row.className = "reply-image-preview-item";
          const image = document.createElement("img");
          image.alt = "已选择的回复图片预览";
          const name = document.createElement("span");
          name.textContent = `${file.name} · ${Math.max(1, Math.ceil(file.size / 1024))} KiB`;
          if (file.type.startsWith("image/")) {
            const objectUrl = URL.createObjectURL(file);
            previewUrls.push(objectUrl);
            image.src = objectUrl;
          }
          row.append(image, name);
          preview.append(row);
        });
      });
      replyForm.addEventListener("reset", clearPreview);
      window.addEventListener("pagehide", clearPreview, { once: true });
    }
    replyForm.addEventListener("reset", () => {
      if (imageInput instanceof HTMLInputElement) imageInput.setCustomValidity("");
      if (submitButton instanceof HTMLButtonElement) {
        submitButton.textContent = defaultSubmitLabel;
      }
    });
  });

  // The root-owned updater may restart joker-web while this page is open.
  // Poll only the administrator-scoped status endpoint, ignore the short
  // connection gap during restart, and stop once the worker reaches a final
  // state.  No update authorization or package contents are exposed here.
  const updateMonitor = document.querySelector("[data-update-monitor]");
  if (updateMonitor instanceof HTMLElement && updateMonitor.dataset.monitorActive === "1") {
    const statusUrl = updateMonitor.dataset.statusUrl || "/admin/update/status";
    const stateNodes = document.querySelectorAll("[data-update-state]");
    const messageNode = document.querySelector("[data-update-message]");
    const timeNode = document.querySelector("[data-update-time]");
    const packageNode = document.querySelector("[data-update-package]");
    const packageVersionNode = document.querySelector("[data-update-release-version]");
    const installedVersionNodes = document.querySelectorAll("[data-installed-release-version]");
    const installedPackageNode = document.querySelector("[data-installed-release-package]");
    const installedTimeNode = document.querySelector("[data-installed-release-time]");
    const installedDetailNode = document.querySelector("[data-installed-release-detail]");
    let stopped = false;
    let timer = 0;
    const schedule = () => {
      if (!stopped) timer = window.setTimeout(poll, 4000);
    };
    const poll = async () => {
      if (stopped) return;
      try {
        const response = await fetch(statusUrl, {
          credentials: "same-origin",
          cache: "no-store",
          redirect: "error",
          headers: { "Accept": "application/json" },
        });
        if (!response.ok) return schedule();
        const payload = await response.json();
        const state = typeof payload.state === "string" ? payload.state.slice(0, 32) : "unknown";
        const message = typeof payload.message === "string" ? payload.message.slice(0, 300) : "";
        const updatedAt = typeof payload.updated_at === "string" ? payload.updated_at.slice(0, 64) : "";
        const packageName = typeof payload.package === "string" ? payload.package.slice(0, 160) : "";
        const packageVersion = typeof payload.release_version === "string" ? payload.release_version.slice(0, 64) : "";
        const installedRelease = payload.installed_release && typeof payload.installed_release === "object" ? payload.installed_release : {};
        const installedVersion = typeof installedRelease.version === "string" ? installedRelease.version.slice(0, 64) : "未记录";
        const installedPackage = typeof installedRelease.package === "string" ? installedRelease.package.slice(0, 160) : "";
        const installedAt = typeof installedRelease.installed_at === "string" ? installedRelease.installed_at.slice(0, 64) : "";
        const installedDetail = typeof installedRelease.message === "string" ? installedRelease.message.slice(0, 180) : "";
        stateNodes.forEach((node) => { node.textContent = state; });
        if (messageNode) messageNode.textContent = message;
        if (timeNode) timeNode.textContent = updatedAt || "暂无";
        if (packageNode) packageNode.textContent = packageName || "暂无";
        if (packageVersionNode) packageVersionNode.textContent = packageVersion || "等待服务器验证";
        installedVersionNodes.forEach((node) => { node.textContent = installedVersion; });
        if (installedPackageNode) installedPackageNode.textContent = installedPackage || "未记录";
        if (installedTimeNode) installedTimeNode.textContent = installedAt || "未记录";
        if (installedDetailNode) installedDetailNode.textContent = installedDetail;
        if (["success", "failed", "idle"].includes(state)) {
          stopped = true;
          return;
        }
      } catch (_) {
        // A service restart or a transient proxy error is expected here.
      }
      schedule();
    };
    schedule();
    window.addEventListener("pagehide", () => {
      stopped = true;
      window.clearTimeout(timer);
    }, { once: true });
  }
})();
