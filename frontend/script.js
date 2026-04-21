const searchInput = document.getElementById("searchInput");
const extensionInput = document.getElementById("extensionInput");
const refreshButton = document.getElementById("refreshButton");
const filesBody = document.getElementById("filesBody");
const statusLine = document.getElementById("status");
const emptyRowTemplate = document.getElementById("emptyRowTemplate");
const rootUriMeta = document.querySelector('meta[name="app-root-uri"]');

let debounceTimer = null;
const APP_ROOT_URI = normalizeRootUri(rootUriMeta?.content ?? "");

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

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function buildRow(file) {
  const openedAt = file.opened_at ?? "-";
  return `
    <tr>
      <td>${escapeHtml(file.filename)}</td>
      <td class="path-cell" title="${escapeHtml(file.filepath)}">${escapeHtml(file.filepath)}</td>
      <td>${escapeHtml(file.user)}</td>
      <td>${escapeHtml(file.pid)}</td>
      <td>${escapeHtml(openedAt)}</td>
      <td>
        <button class="close-button" data-pid="${escapeHtml(file.pid)}">Close</button>
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

async function fetchFiles({ forceRefresh = false } = {}) {
  const searchValue = searchInput.value.trim();
  const extensionValue = extensionInput.value.trim();

  const params = new URLSearchParams();
  if (searchValue) {
    params.set("search", searchValue);
  }
  if (extensionValue) {
    params.set("extension", extensionValue);
  }
  if (forceRefresh) {
    params.set("refresh", "true");
  }

  setStatus("Loading...");
  try {
    const query = params.toString();
    const response = await fetch(buildApiUrl("files", query ? params : null));
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.message || payload.detail || "Failed to load files");
    }

    renderFiles(payload.files || []);
    setStatus(`Open files found: ${payload.count ?? 0}`);
  } catch (error) {
    renderFiles([]);
    setStatus(error.message || "Failed to load files", true);
  }
}

async function closeConnection(pid) {
  const shouldClose = window.confirm("Are you sure you want to close this Samba connection?");
  if (!shouldClose) {
    return;
  }

  setStatus(`Closing Samba connection for PID ${pid}...`);
  try {
    const response = await fetch(buildApiUrl(`close/${pid}`), { method: "POST" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.message || payload.detail || "Failed to close Samba connection");
    }

    await fetchFiles({ forceRefresh: true });
    setStatus(payload.message || `Samba connection for PID ${pid} closed.`);
  } catch (error) {
    setStatus(error.message || "Failed to close Samba connection", true);
  }
}

function scheduleLiveSearch() {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => {
    fetchFiles();
  }, 250);
}

searchInput.addEventListener("input", scheduleLiveSearch);
extensionInput.addEventListener("input", scheduleLiveSearch);
refreshButton.addEventListener("click", () => fetchFiles({ forceRefresh: true }));

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
    setStatus("Invalid PID", true);
    return;
  }

  closeConnection(pid);
});

fetchFiles({ forceRefresh: true });
