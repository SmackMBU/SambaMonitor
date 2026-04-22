const searchInput = document.getElementById("searchInput");
const filesOnlyInput = document.getElementById("filesOnlyInput");
const refreshButton = document.getElementById("refreshButton");
const filesBody = document.getElementById("filesBody");
const statusLine = document.getElementById("status");
const emptyRowTemplate = document.getElementById("emptyRowTemplate");
const rootUriMeta = document.querySelector('meta[name="app-root-uri"]');
const pagination = document.getElementById("pagination");
const pageInfo = document.getElementById("pageInfo");
const prevPageButton = document.getElementById("prevPageButton");
const nextPageButton = document.getElementById("nextPageButton");

const PAGE_SIZE = 40;

let debounceTimer = null;
let activeFetchController = null;

const APP_ROOT_URI = normalizeRootUri(rootUriMeta?.content ?? "");
const state = {
  allFiles: [],
  filteredFiles: [],
  syncedAt: null,
  currentPage: 1,
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

function getLeafName(value) {
  const normalized = String(value ?? "").trim().replace(/[\\/]+$/, "");
  if (!normalized) {
    return "";
  }

  const parts = normalized.split(/[\\/]+/).filter(Boolean);
  if (!parts.length) {
    return normalized;
  }
  return parts[parts.length - 1];
}

function toDisplayName(filename, filepath) {
  const rawFilename = String(filename ?? "").trim();
  const rawFilepath = String(filepath ?? "").trim();

  if (rawFilename && rawFilename !== "." && rawFilename !== ".." && !/[\\/]/.test(rawFilename)) {
    return rawFilename;
  }

  const filenameLeaf = getLeafName(rawFilename);
  if (filenameLeaf && filenameLeaf !== "." && filenameLeaf !== "..") {
    return filenameLeaf;
  }

  const filepathLeaf = getLeafName(rawFilepath);
  if (filepathLeaf && filepathLeaf !== "." && filepathLeaf !== "..") {
    return filepathLeaf;
  }

  return rawFilename || rawFilepath || "(без имени)";
}

function hasExtension(name) {
  const value = String(name ?? "").trim();
  if (!value) {
    return false;
  }

  const dotIndex = value.lastIndexOf(".");
  if (dotIndex <= 0) {
    return false;
  }
  return dotIndex < value.length - 1;
}

function prepareFiles(rawFiles) {
  return rawFiles.map((rawFile) => {
    const filename = String(rawFile?.filename ?? "");
    const filepath = String(rawFile?.filepath ?? "");
    const displayName = toDisplayName(filename, filepath);
    const user = String(rawFile?.user ?? "unknown");
    const pid = String(rawFile?.pid ?? "");
    const openedAt = rawFile?.opened_at == null ? "-" : String(rawFile.opened_at);

    return {
      ...rawFile,
      filename,
      filepath,
      displayName,
      user,
      pid,
      openedAt,
      _searchText: `${displayName} ${filename} ${filepath} ${user} ${pid}`.toLowerCase(),
    };
  });
}

function buildStatusText({ totalAll, totalFiltered, from, to, hideWithoutExtensionEnabled }) {
  const rangeText = totalFiltered > 0 ? `${from}-${to}` : "0";
  const syncText = state.syncedAt ? ` Обновлено: ${state.syncedAt.toLocaleTimeString("ru-RU")}.` : "";
  const modeText = hideWithoutExtensionEnabled ? " Режим: скрывать без расширения." : "";
  return `Показано ${rangeText} из ${totalFiltered} (всего ${totalAll}) открытых файлов.${syncText}${modeText}`;
}

function buildRow(file) {
  const details = [];
  if (file.filename && file.filename !== file.displayName) {
    details.push(`Исходное имя: ${file.filename}`);
  }
  if (file.filepath) {
    details.push(`Путь: ${file.filepath}`);
  }
  const tooltip = details.join("\n") || file.displayName;

  return `
    <tr>
      <td class="cell-filename" title="${escapeHtml(tooltip)}">${escapeHtml(file.displayName)}</td>
      <td class="cell-user">${escapeHtml(file.user)}</td>
      <td class="cell-pid">${escapeHtml(file.pid)}</td>
      <td class="cell-opened">${escapeHtml(file.openedAt)}</td>
      <td class="cell-action">
        <button class="close-button" data-pid="${escapeHtml(file.pid)}" type="button">Закрыть</button>
      </td>
    </tr>
  `;
}

function renderFiles(files) {
  if (!files.length) {
    filesBody.innerHTML = "";
    filesBody.append(emptyRowTemplate.content.cloneNode(true));
    return;
  }
  filesBody.innerHTML = files.map(buildRow).join("");
}

function updatePagination(totalPages, totalFiltered) {
  if (totalFiltered <= PAGE_SIZE || totalPages <= 1) {
    pagination.hidden = true;
    return;
  }

  pagination.hidden = false;
  pageInfo.textContent = `Страница ${state.currentPage} из ${totalPages}`;
  prevPageButton.disabled = state.currentPage <= 1;
  nextPageButton.disabled = state.currentPage >= totalPages;
}

function renderCurrentPage() {
  const totalAll = state.allFiles.length;
  const totalFiltered = state.filteredFiles.length;
  const hideWithoutExtensionEnabled = Boolean(filesOnlyInput?.checked);
  const totalPages = Math.max(1, Math.ceil(totalFiltered / PAGE_SIZE));

  state.currentPage = Math.min(Math.max(1, state.currentPage), totalPages);

  const startIndex = totalFiltered === 0 ? 0 : (state.currentPage - 1) * PAGE_SIZE;
  const endIndex = Math.min(startIndex + PAGE_SIZE, totalFiltered);
  const pageItems = state.filteredFiles.slice(startIndex, endIndex);

  renderFiles(pageItems);
  updatePagination(totalPages, totalFiltered);
  setStatus(
    buildStatusText({
      totalAll,
      totalFiltered,
      from: totalFiltered === 0 ? 0 : startIndex + 1,
      to: endIndex,
      hideWithoutExtensionEnabled,
    })
  );
}

function applyFilters({ resetPage = true } = {}) {
  const searchValue = searchInput.value.trim().toLowerCase();
  const hideWithoutExtensionEnabled = Boolean(filesOnlyInput?.checked);

  let filtered = state.allFiles;
  if (searchValue) {
    filtered = filtered.filter((file) => file._searchText.includes(searchValue));
  }
  if (hideWithoutExtensionEnabled) {
    filtered = filtered.filter((file) => hasExtension(file.displayName));
  }
  state.filteredFiles = filtered;

  if (resetPage) {
    state.currentPage = 1;
  }

  renderCurrentPage();
}

function scheduleLiveFilter() {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => {
    applyFilters({ resetPage: true });
  }, 160);
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
    applyFilters({ resetPage: true });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      return;
    }

    state.allFiles = [];
    state.filteredFiles = [];
    state.currentPage = 1;
    renderFiles([]);
    pagination.hidden = true;
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
filesOnlyInput?.addEventListener("change", () => applyFilters({ resetPage: true }));
refreshButton.addEventListener("click", () => refreshFiles({ forceRefresh: true }));

prevPageButton.addEventListener("click", () => {
  if (state.currentPage <= 1) {
    return;
  }
  state.currentPage -= 1;
  renderCurrentPage();
});

nextPageButton.addEventListener("click", () => {
  const totalPages = Math.max(1, Math.ceil(state.filteredFiles.length / PAGE_SIZE));
  if (state.currentPage >= totalPages) {
    return;
  }
  state.currentPage += 1;
  renderCurrentPage();
});

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
