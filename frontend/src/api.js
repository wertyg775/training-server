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

// Prepared for the upload flow; the Upload Files button is intentionally unwired.
export function uploadProject(name, file, signal) {
  const body = new FormData();
  body.append('name', name);
  body.append('file', file);
  return request('/upload', { method: 'POST', body, signal });
}
