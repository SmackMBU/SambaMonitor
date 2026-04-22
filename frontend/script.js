const searchInput = document.getElementById("searchInput");
const extensionInput = document.getElementById("extensionInput");
const refreshButton = document.getElementById("refreshButton");
const filesBody = document.getElementById("filesBody");
const statusLine = document.getElementById("status");
const emptyRowTemplate = document.getElementById("emptyRowTemplate");
const rootUriMeta = document.querySelector('meta[name="app-root-uri"]');

let debounceTimer = null;
let activeFetchController = null;
let renderJobId = 0;

const APP_ROOT_URI = normalizeRootUri(rootUriMeta?.content ?? "");
const state = {
  allFiles: [],
  filteredFiles: [],
  syncedAt: null,
};

function normalizeRootUri(value) {
  const trimmed = String(value ?? "").trim();
  if (!trimmed || trimmed === "/") {
    return "";
  }
  const prefixed = trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
  return prefixed.replace(/\/+$/, "");
}

function buildApiUrl(path, queryParams = null) {
  const normalizedPath = String(path).replace(/^\/+/, "");
  const url = new URL(`${APP_ROOT_URI}/${normalizedPath}`, window.location.origin);
  if (queryParams) {
    url.search = queryParams.toString();
  }
  return url.toString();
}

function setStatus(text, isError = false) {
  statusLine.textContent = text;
  statusLine.classList.toggle("error", isError);
}

function setRefreshLoading(isLoading) {
  refreshButton.disabled = isLoading;
  refreshButton.classList.toggle("is-loading", isLoading);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function splitMasks(value) {
  const normalized = String(value ?? "").trim();
  if (!normalized) {
    return [];
  }
  return normalized
    .split(/[;,]/)
    .map((part) => part.trim())
    .filter(Boolean);
}

function containsWildcard(mask) {
  return /[*?\[\]]/.test(mask);
}

function escapeRegexFragment(value) {
  return value.replace(/[|\\{}()[\]^$+*?.]/g, "\\$&");
}

function wildcardToRegex(mask) {
  let regex = "^";
  for (let i = 0; i < mask.length; i += 1) {
    const char = mask[i];
    if (char === "*") {
      regex += ".*";
      continue;
    }
    if (char === "?") {
      regex += ".";
      continue;
    }
    if (char === "[") {
      const end = mask.indexOf("]", i + 1);
      if (end === -1) {
        regex += "\\[";
        continue;
      }

      let charClass = mask.slice(i + 1, end);
      if (!charClass) {
        regex += "\\[\\]";
        i = end;
        continue;
      }

      let prefix = "";
      if (charClass[0] === "!") {
        prefix = "^";
        charClass = charClass.slice(1);
      } else if (charClass[0] === "^") {
        prefix = "\\^";
        charClass = charClass.slice(1);
      }

      charClass = charClass.replace(/\\/g, "\\\\").replace(/]/g, "\\]");
      regex += `[${prefix}${charClass}]`;
      i = end;
      continue;
    }
    regex += escapeRegexFragment(char);
  }
  regex += "$";
  return new RegExp(regex, "i");
}

function normalizeExtensionMask(mask) {
  const normalized = mask.trim();
  if (!normalized) {
    return "";
  }
  if (containsWildcard(normalized)) {
    return normalized;
  }
  if (normalized.startsWith(".")) {
    return `*${normalized}`;
  }
  return `*.${normalized}`;
}

function createSearchMatcher(rawValue) {
  const masks = splitMasks(rawValue);
  if (!masks.length) {
    return () => true;
  }

  const wildcardRegexList = masks
    .filter(containsWildcard)
    .map(wildcardToRegex);
  const loweredTerms = masks
    .filter((mask) => !containsWildcard(mask))
    .map((mask) => mask.toLowerCase());

  return (file) => {
    const wildcardMatch = wildcardRegexList.some(
      (regex) => regex.test(file.filename) || regex.test(file.filepath)
    );
    const substringMatch = loweredTerms.some((term) => file._searchText.includes(term));
    return wildcardMatch || substringMatch;
  };
}

function createExtensionMatcher(rawValue) {
  const masks = splitMasks(rawValue)
    .map(normalizeExtensionMask)
    .filter(Boolean);

  if (!masks.length) {
    return () => true;
  }

  const regexList = masks.map(wildcardToRegex);
  return (file) => regexList.some((regex) => regex.test(file.filename));
}

function prepareFiles(rawFiles) {
  return rawFiles.map((rawFile) => {
    const filename = String(rawFile?.filename ?? "");
    const filepath = String(rawFile?.filepath ?? "");
    return {
      ...rawFile,
      filename,
      filepath,
      user: String(rawFile?.user ?? "unknown"),
      _searchText: `${filename} ${filepath}`.toLowerCase(),
    };
  });
}

function buildStatusText() {
  const total = state.allFiles.length;
  const shown = state.filteredFiles.length;
  const syncText = state.syncedAt
    ? ` Обновлено: ${state.syncedAt.toLocaleTimeString("ru-RU")}.`
    : "";
  return `Показано ${shown} из ${total} открытых файлов.${syncText}`;
}

function buildRow(file) {
  const openedAt = file.opened_at ?? "-";
  return `
    <tr>
      <td class="cell-filename">${escapeHtml(file.filename)}</td>
      <td class="path-cell">
        <code class="path-value" title="${escapeHtml(file.filepath)}">${escapeHtml(file.filepath)}</code>
      </td>
      <td>${escapeHtml(file.user)}</td>
      <td>${escapeHtml(file.pid)}</td>
      <td>${escapeHtml(openedAt)}</td>
      <td>
        <button class="close-button" data-pid="${escapeHtml(file.pid)}">Закрыть</button>
      </td>
    </tr>
  `;
}

function renderFiles(files) {
  const localRenderJobId = ++renderJobId;
  filesBody.innerHTML = "";

  if (!files.length) {
    filesBody.append(emptyRowTemplate.content.cloneNode(true));
    return;
  }

  const chunkSize = files.length > 2000 ? 120 : 320;
  let index = 0;

  function renderChunk() {
    if (localRenderJobId !== renderJobId) {
      return;
    }

    const end = Math.min(index + chunkSize, files.length);
    let chunkHtml = "";
    for (; index < end; index += 1) {
      chunkHtml += buildRow(files[index]);
    }
    filesBody.insertAdjacentHTML("beforeend", chunkHtml);

    if (index < files.length) {
      window.requestAnimationFrame(renderChunk);
    }
  }

  window.requestAnimationFrame(renderChunk);
}

function applyFiltersAndRender() {
  const searchMatcher = createSearchMatcher(searchInput.value);
  const extensionMatcher = createExtensionMatcher(extensionInput.value);

  state.filteredFiles = state.allFiles.filter(
    (file) => searchMatcher(file) && extensionMatcher(file)
  );

  renderFiles(state.filteredFiles);
  setStatus(buildStatusText());
}

function scheduleLiveFilter() {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => {
    applyFiltersAndRender();
  }, 120);
}

async function refreshFiles({ forceRefresh = false } = {}) {
  if (activeFetchController) {
    activeFetchController.abort();
  }

  const controller = new AbortController();
  activeFetchController = controller;
  setRefreshLoading(true);
  setStatus("Загружаю данные с сервера...");

  const params = new URLSearchParams();
  if (forceRefresh) {
    params.set("refresh", "true");
  }

  try {
    const response = await fetch(buildApiUrl("files", params.toString() ? params : null), {
      signal: controller.signal,
      cache: "no-store",
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.message || payload.detail || "Не удалось загрузить файлы");
    }

    const files = Array.isArray(payload.files) ? payload.files : [];
    state.allFiles = prepareFiles(files);
    state.syncedAt = new Date();
    applyFiltersAndRender();
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      return;
    }

    state.allFiles = [];
    state.filteredFiles = [];
    renderFiles([]);
    setStatus(error.message || "Не удалось загрузить файлы", true);
  } finally {
    if (activeFetchController === controller) {
      activeFetchController = null;
    }
    setRefreshLoading(false);
  }
}

async function closeConnection(pid, triggerButton) {
  const shouldClose = window.confirm("Закрыть это Samba-подключение?");
  if (!shouldClose) {
    return;
  }

  if (triggerButton) {
    triggerButton.disabled = true;
  }

  setStatus(`Закрываю подключение для PID ${pid}...`);
  try {
    const response = await fetch(buildApiUrl(`close/${pid}`), { method: "POST" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.message || payload.detail || "Не удалось закрыть подключение");
    }

    await refreshFiles({ forceRefresh: true });
    setStatus(payload.message || `Подключение Samba для PID ${pid} закрыто.`);
  } catch (error) {
    setStatus(error.message || "Не удалось закрыть подключение", true);
  } finally {
    if (triggerButton) {
      triggerButton.disabled = false;
    }
  }
}

searchInput.addEventListener("input", scheduleLiveFilter);
extensionInput.addEventListener("input", scheduleLiveFilter);
refreshButton.addEventListener("click", () => refreshFiles({ forceRefresh: true }));

filesBody.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) {
    return;
  }
  if (!target.classList.contains("close-button")) {
    return;
  }

  const pid = Number(target.dataset.pid);
  if (!Number.isInteger(pid) || pid <= 0) {
    setStatus("Некорректный PID", true);
    return;
  }

  closeConnection(pid, target);
});

refreshFiles({ forceRefresh: true });
