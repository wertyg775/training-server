import React, { useEffect, useRef, useState } from 'react';
import { listReadyProjects } from './api.js';
import '../styles.css';

function importedAt(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '-';
  const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
  if (seconds < 60) return 'Just now';
  const [amount, unit] = seconds < 3600
    ? [Math.floor(seconds / 60), 'minute']
    : seconds < 86400
      ? [Math.floor(seconds / 3600), 'hour']
      : [Math.floor(seconds / 86400), 'day'];
  return `${amount} ${unit}${amount === 1 ? '' : 's'} ago`;
}

export default function ProjectsPage() {
  const [projects, setProjects] = useState([]);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState(new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [isUploadModalOpen, setIsUploadModalOpen] = useState(false);
  const selectAll = useRef(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError('');
    listReadyProjects(controller.signal)
      .then((data) => { if (!controller.signal.aborted) setProjects(data); })
      .catch((failure) => { if (!controller.signal.aborted) setError(failure.message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [attempt]);

  const visible = projects.filter((project) =>
    project.name.toLowerCase().includes(query.trim().toLowerCase()));
  const allSelected = visible.length > 0 && visible.every((project) => selected.has(project.id));
  const someSelected = visible.some((project) => selected.has(project.id));

  useEffect(() => {
    selectAll.current.indeterminate = someSelected && !allSelected;
  }, [someSelected, allSelected]);

  useEffect(() => {
    if (!isUploadModalOpen) return undefined;
    const closeOnEscape = (event) => {
      if (event.key === 'Escape') setIsUploadModalOpen(false);
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [isUploadModalOpen]);

  function toggleSelection(ids, checked) {
    setSelected((previous) => {
      const next = new Set(previous);
      ids.forEach((id) => checked ? next.add(id) : next.delete(id));
      return next;
    });
  }

  return (
    <>
      <header className="brand">Training Server</header>
      <div className="layout">
        <aside className="sidebar">
          <nav className="nav" aria-label="Main">
            <a className="nav-item active" href="#" aria-current="page">Projects</a>
            <a className="nav-item" href="#">Training Jobs</a>
            <a className="nav-item" href="#">Executions</a>
            <a className="nav-item" href="#">Settings</a>
          </nav>
        </aside>
        <div className="content">
          <main className="dashboard">
            <div className="page-head">
              <h1>Projects</h1>
              <button className="upload-button" type="button" onClick={() => setIsUploadModalOpen(true)}><span className="plus-icon" aria-hidden="true">+</span> Upload Files</button>
            </div>
            <section aria-label="Ready projects" aria-busy={loading}>
              <input className="project-search" type="search" aria-label="Search projects" placeholder="Search projects" value={query} onChange={(event) => setQuery(event.target.value)} />
              <table className="projects-table">
                <thead>
                  <tr>
                    <th className="col-check"><input ref={selectAll} type="checkbox" aria-label="Select all projects" checked={allSelected} disabled={!visible.length} onChange={(event) => toggleSelection(visible.map((project) => project.id), event.target.checked)} /></th>
                    <th>Name</th><th>Source</th><th>Repository</th><th>Revision</th><th>Commit</th><th>Imported</th><th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {loading ? <tr><td colSpan={8} role="status">Loading projects…</td></tr>
                    : error ? <tr><td colSpan={8}><span role="alert">{error}</span> <button type="button" onClick={() => setAttempt((value) => value + 1)}>Retry</button></td></tr>
                      : visible.length === 0 ? <tr><td colSpan={8} role="status">{query.trim() ? 'No matching projects.' : 'No ready projects yet.'}</td></tr>
                        : visible.map((project) => (
                          <tr key={project.id}>
                            <td><input type="checkbox" aria-label={`Select ${project.name}`} checked={selected.has(project.id)} onChange={(event) => toggleSelection([project.id], event.target.checked)} /></td>
                            <td className="name" title={project.name}>{project.name}</td>
                            <td>{project.source_type === 'git' ? 'Git' : 'Upload'}</td>
                            <td className="repo" title={project.repository_url}>{project.repository_url?.replace(/^https:\/\//, '') || '-'}</td>
                            <td>{project.requested_revision || '-'}</td>
                            <td title={project.resolved_commit}>{project.resolved_commit?.slice(0, 7) || '-'}</td>
                            <td title={project.created_at}>{importedAt(project.created_at)}</td>
                            <td>-</td>
                          </tr>
                        ))}
                </tbody>
              </table>
            </section>
          </main>
        </div>
      </div>
      {isUploadModalOpen && (
        <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setIsUploadModalOpen(false);
        }}>
          <section className="upload-modal" role="dialog" aria-modal="true" aria-label="Upload files">
            <button className="modal-close" type="button" aria-label="Close upload dialog" onClick={() => setIsUploadModalOpen(false)}>×</button>
          </section>
        </div>
      )}
    </>
  );
}
