async function request(path, options = {}) {
  const response = await fetch(`/api/projects${path}`, options);
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data?.error || data?.detail;
    throw new Error(typeof detail === 'string' ? detail : `Request failed (${response.status}).`);
  }
  return data;
}

export async function listReadyProjects(signal) {
  const projects = await request('/ready', { signal });
  return projects.filter((project) => project.status === 'ready');
}

export function readProjectFile(projectId, path, signal) {
  return request(`/${encodeURIComponent(projectId)}/file?${new URLSearchParams({ path })}`, { signal });
}

export function submitTraining(projectId, entrypoint, epochs) {
  return request(`/${encodeURIComponent(projectId)}/training-jobs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ entrypoint, epochs }),
  });
}

export function listProjectFiles(projectId, path = '', signal) {
  const query = path ? `?${new URLSearchParams({ path })}` : '';
  return request(`/${encodeURIComponent(projectId)}/files${query}`, { signal });
}

// Prepared for the upload flow; the Upload Files button is intentionally unwired.
export function uploadProject(name, file, signal) {
  const body = new FormData();
  body.append('name', name);
  body.append('file', file);
  return request('/upload', { method: 'POST', body, signal });
}
